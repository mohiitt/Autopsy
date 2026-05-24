"""
Financial Research Pipeline - autopsy complex multi-agent demo.

A production-realistic simulation of a hedge fund's nightly AI research pipeline:
  orchestrator -> ticker-planner -> 3x parallel researchers -> synthesizer
                -> risk-checker -> report-writer -> publisher

This example is designed to showcase every autopsy feature:
  - Multi-level agent DAG (3 levels deep, branching, parallel)
  - Real LLM calls via GMI Cloud (falls back to fake responses if no API key)
  - Tool nodes (SEC filing parser, web search, options data)
  - Agent handoffs between stages
  - Realistic failure modes (context_overflow -> cascade failure)
  - Live streaming to autopsy dashboard
  - AI-powered root cause diagnosis
  - Time-travel replay

Run with:
    autopsy run examples/financial_research_pipeline.py

Or directly:
    python examples/financial_research_pipeline.py
    python examples/financial_research_pipeline.py "Research semiconductor plays"

Environment:
    AUTOPSY_DEMO_MODE=broken   (default) - pipeline fails at synthesizer
    AUTOPSY_DEMO_MODE=success  - pipeline succeeds end-to-end
    AUTOPSY_DEMO_MODE=timeout  - researcher-1 times out after 5 seconds
    GMI_API_KEY=...            - if set, makes real LLM calls; otherwise uses fake responses
    GMI_DEFAULT_MODEL=...      - default: deepseek-ai/DeepSeek-V3.2
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path

from openai import AsyncOpenAI

from autopsy import lens

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

DEMO_MODE = os.environ.get("AUTOPSY_DEMO_MODE", "broken")
# "broken"  -> pipeline fails at synthesizer (context_overflow -> bad_json cascade)
# "success" -> pipeline succeeds end-to-end (use for happy-path dashboard demo)
# "timeout" -> researcher-1 times out (asyncio.TimeoutError after 5s)
# "loop"    -> default: continuously run; auto-flip broken->success when the
#              user clicks "Apply fix & replay" in the dashboard.

# Multiplier on simulated latencies so the live dashboard build-up is visible.
# Default 2.5x makes each iteration take ~10-15s so users can WATCH the
# flowchart fill in node by node before the failure happens.
LATENCY_SCALE = float(os.environ.get("AUTOPSY_LATENCY_SCALE", "2.5"))

# Fix marker file: when the dashboard / user signals that the fix has been
# applied, this file is created. The pipeline checks for it on every iteration
# and switches from broken -> success mode while it exists.
FIX_MARKER_PATH = Path(
    os.environ.get(
        "AUTOPSY_FIX_MARKER",
        str(Path.home() / ".autopsy" / "fix_applied"),
    )
).expanduser()
# Also accept a workspace-local marker (used by the FastAPI server route).
FIX_MARKER_PATH_LOCAL = Path.cwd() / ".autopsy" / "fix_applied"


def _is_fix_applied() -> bool:
    """Return True when the user has clicked 'Apply fix & replay' on the dashboard."""
    try:
        return FIX_MARKER_PATH.exists() or FIX_MARKER_PATH_LOCAL.exists()
    except Exception:
        return False


def _effective_mode() -> str:
    """Resolve the current demo mode, honoring the live fix-marker.

    Priority:
      1. AUTOPSY_DEMO_MODE=success/timeout/bad_json -> explicit overrides
      2. fix marker present -> "success" (the fix has been applied live)
      3. DEMO_MODE env (default "broken")
    """
    mode = DEMO_MODE
    if mode in ("success", "timeout", "bad_json"):
        return mode
    if _is_fix_applied():
        return "success"
    return mode  # "broken" or any custom


GMI_CLIENT = AsyncOpenAI(
    api_key=os.environ.get("GMI_API_KEY", "") or "sk-no-key",
    base_url=os.environ.get("GMI_BASE_URL", "https://api.gmi-serving.com/v1"),
    timeout=12.0,
)
MODEL = os.environ.get("GMI_DEFAULT_MODEL", "deepseek-ai/DeepSeek-V3.2")
HAS_GMI_KEY = bool(os.environ.get("GMI_API_KEY"))

# --------------------------------------------------------------------------- #
# Fake LLM responses (used when GMI_API_KEY is not set)                       #
# Keyed by a short prompt-prefix tag so the right response is returned.       #
# --------------------------------------------------------------------------- #

FAKE_RESPONSES: dict[str, str] = {
    "ticker_planner": json.dumps({"tickers": ["NVDA", "MSFT", "GOOGL"]}),
    "researcher_NVDA": json.dumps({
        "ticker": "NVDA", "rating": "BUY", "target_price": 165.0,
        "thesis": "NVIDIA remains the structural winner of the AI compute "
                  "buildout. Blackwell ramp doubles per-rack training "
                  "throughput while CUDA moat keeps switching costs near-"
                  "prohibitive. Hyperscaler capex commitments through 2026 "
                  "imply $90B+ Data Center run rate.",
        "key_metrics": {"revenue_growth_yoy": 1.22, "gross_margin": 0.746,
                        "fcf_b": 33.5, "data_center_share": 0.78},
        "risks": ["China export controls (~$4B annual)",
                  "Customer concentration (top 3 = 32% of revenue)",
                  "Cyclicality of AI training capex"],
    }),
    "researcher_MSFT": json.dumps({
        "ticker": "MSFT", "rating": "BUY", "target_price": 495.0,
        "thesis": "Best-positioned to monetize generative AI across both "
                  "infrastructure (Azure OpenAI) and application layers "
                  "(M365 Copilot). $80B FY25 capex guidance is digestible "
                  "given $74B FCF and $259B backlog.",
        "key_metrics": {"revenue_growth_yoy": 0.16, "operating_margin": 0.446,
                        "fcf_b": 74.1, "azure_growth_yoy": 0.30},
        "risks": ["AI capex commitment scrutiny",
                  "Activision integration regulatory",
                  "Storm-0558 cybersecurity remediation"],
    }),
    "researcher_GOOGL": json.dumps({
        "ticker": "GOOGL", "rating": "HOLD", "target_price": 195.0,
        "thesis": "Search remains a cash machine but DOJ remedy phase and "
                  "Gen-AI cannibalization both peak in 2025. Cloud is the "
                  "bright spot but lags Azure/AWS in enterprise wins. "
                  "Antitrust overhang caps multiple expansion.",
        "key_metrics": {"revenue_growth_yoy": 0.09, "operating_margin": 0.27,
                        "fcf_b": 69.5, "cloud_growth_yoy": 0.26},
        "risks": ["DOJ search remedy (Chrome divestiture tail risk)",
                  "AI search cannibalization (SGE monetization gap)",
                  "EU DMA enforcement"],
    }),
    "synthesizer": json.dumps({
        "portfolio_thesis": "Long AI compute (NVDA) and AI monetization "
                            "(MSFT) on conviction; underweight GOOGL on "
                            "antitrust + search cannibalization risk.",
        "top_pick": "NVDA",
        "risk_adjusted_return_pct": 18.5,
        "allocation": {"NVDA": 0.45, "MSFT": 0.40, "GOOGL": 0.15},
    }),
    "risk_checker": json.dumps({
        "var_95_pct": 6.8, "max_drawdown_pct": 14.2,
        "concentration_risk": "elevated (85% in semis + cloud)",
        "tail_risk_flags": ["Taiwan strait geopolitical",
                            "AI capex retrenchment cycle",
                            "DOJ Chrome divestiture (GOOGL)"],
        "overall_risk_score": 6,
    }),
    "report_writer": (
        "**Morning Briefing - AI Infrastructure Portfolio Recommendation**\n\n"
        "We recommend a 45/40/15 allocation to NVDA/MSFT/GOOGL with a "
        "risk-adjusted expected return of 18.5% over the next 12 months. "
        "NVDA remains our top pick on the back of the Blackwell ramp "
        "(+217% Data Center growth, $90B+ FY25 run rate) and a CUDA moat "
        "that keeps switching costs near-prohibitive.\n\n"
        "MSFT is the highest-quality way to play AI monetization: $80B "
        "FY25 capex is digestible given $74B FCF and a $259B backlog, "
        "and the M365 Copilot seat ramp (60% QoQ) provides revenue "
        "diversification beyond Azure. We are underweight GOOGL relative "
        "to the cap-weighted benchmark on combined DOJ remedy and Gen-AI "
        "search-cannibalization risk.\n\n"
        "Portfolio-level VaR(95) is 6.8% with a 14.2% expected max drawdown; "
        "the principal tail risks are Taiwan strait geopolitics, an AI "
        "capex retrenchment cycle, and the Chrome divestiture scenario. "
        "Position sizing reflects an overall risk score of 6/10."
    ),
}

# --------------------------------------------------------------------------- #
# Realistic fake data for tool returns                                        #
# --------------------------------------------------------------------------- #

FAKE_SEC_FILINGS = {
    "NVDA": """NVIDIA Corporation (NVDA) - 10-K Annual Report Excerpt (FY2024)
Revenue: $60.9B (+122% YoY). Data Center segment: $47.5B (+217% YoY).
Gross margin: 74.6%. Net income: $29.8B. EPS: $11.93 diluted.
Key risk factors: Export controls on H100/H200 GPUs to China (~$4B annual impact).
Customer concentration: Microsoft (12%), Google (11%), Meta (9%) represent 32% of revenue.
R&D spend: $8.7B. CapEx: $1.1B. Free cash flow: $33.5B.
Blackwell architecture ramp: production volumes expected to triple in H2 FY2025.
Operating expenses grew 31% YoY to $11.3B, driven by headcount expansion in CUDA
software engineering and accelerated R&D on next-gen Rubin architecture.
Inventory increased 98% YoY to $5.3B reflecting Blackwell pre-build commitments.
Long-term debt: $9.7B with weighted-average coupon of 3.2%, all unsecured.
Stock-based compensation: $3.5B representing 5.7% of revenue.
Geographic mix: US (44%), Singapore (18%), Taiwan (12%), China (17%), other (9%).
Note: China revenue includes products designed to comply with export controls.
Litigation: ongoing patent disputes with three competitors, max exposure $400M.""",
    "MSFT": """Microsoft Corporation (MSFT) - 10-K Annual Report Excerpt (FY2024)
Revenue: $245.1B (+16% YoY). Intelligent Cloud: $105.4B (+20% YoY, Azure +30%).
Productivity & Business Processes: $77.7B (+12%, M365 commercial +14%).
More Personal Computing: $62.0B (+10%, Activision contribution $11.1B).
Operating income: $109.4B with 44.6% margin. Net income: $88.1B.
EPS: $11.80 diluted. Free cash flow: $74.1B. Returned $33.1B to shareholders.
AI infrastructure capex: $55.7B (+58% YoY), guidance $80B+ for FY2025.
Azure OpenAI service: 53,000 enterprise customers, up from 11,000 a year ago.
Copilot for M365 paid seat growth: 60% QoQ in Q4.
Key risks: Concentration in AI capex commitments, regulatory scrutiny on
Activision integration, cybersecurity (Storm-0558 remediation ongoing).
Total cloud commitments backlog: $259B (+20% YoY), of which 56% bookable in 24 months.""",
    "GOOGL": """Alphabet Inc. (GOOGL) - 10-K Annual Report Excerpt (FY2023)
Revenue: $307.4B (+9% YoY). Google Services: $272.5B (Search +11%, YouTube +7%).
Google Cloud: $33.1B (+26% YoY), first full-year of operating profitability ($1.7B).
Other Bets: $1.5B revenue, operating loss of $4.1B (Waymo, Verily, others).
Operating margin: 27% overall, Google Services 35%, Google Cloud 5.2%.
Net income: $73.8B. EPS: $5.80 diluted. Free cash flow: $69.5B.
TAC (traffic acquisition cost): $50.9B (+8%), 22% of advertising revenue.
Generative AI: Gemini Ultra benchmark wins, integrated into Search (SGE),
Workspace (Duet AI), and Cloud (Vertex AI). Capex: $32.3B (+45% YoY).
Antitrust: US v Google search case verdict pending; EU DMA compliance ongoing.
Stock-based comp: $22.5B (7.3% of revenue), buyback authorization $70B.""",
}

FAKE_NEWS = {
    "NVDA": """[1] Nvidia's Blackwell chips face cooling-system delays at hyperscalers (The Information, 2 days ago)
    AWS and Microsoft Azure report retrofit issues with liquid-cooling racks needed for B200 deployment.
    Shipment timing for ~15% of Q1 allocations may slip from March to May 2025.
[2] Nvidia and TSMC announce Arizona Blackwell production milestone (Reuters, 4 days ago)
    First commercial Blackwell wafers from Fab 21 expected to ship in late Q2.
    US manufacturing share of Nvidia's high-end inventory to reach ~5% by end of 2025.
[3] Hedge funds add $4.2B net to NVDA in Q4, 13F filings show (Bloomberg, 1 week ago)
    Tiger Global, Coatue, and Lone Pine all added new or top-up positions.
    Average new-buyer cost basis: $128, vs current price $138.""",
    "MSFT": """[1] Microsoft Azure AI revenue accelerates to $28B annual run rate (Reuters, 2 days ago)
    Azure OpenAI Service now serves 53,000 enterprise customers, up from 11,000 a year ago.
    Copilot integration driving 40% faster enterprise deal cycles.
[2] Microsoft acquires Inflection AI team for $650M in talent deal (Bloomberg, 5 days ago)
    Former DeepMind and Google Brain researchers join Microsoft Research AI division.
    Focus: next-generation reasoning models for enterprise copilot products.
[3] Analysts raise MSFT price target to $520 on AI monetization beat (Morgan Stanley, 1 week ago)
    Azure AI attach rate reaches 34% of new enterprise contracts vs 18% prior year.
    Bull case: $600 if Copilot seats exceed 200M by end of 2025.""",
    "GOOGL": """[1] Google's Gemini 2.5 Pro tops LMArena benchmark, narrowing gap with GPT-5 (Wired, 3 days ago)
    Independent eval shows Gemini 2.5 Pro within 1.2 Elo points of OpenAI's flagship reasoning model.
    Multimodal performance leads on image+code tasks (MathVista, ChartQA).
[2] DOJ proposed remedies in search antitrust case may force Chrome divestiture (WSJ, 6 days ago)
    Hearings begin April 2025; final remedy expected late 2025.
    Analyst estimates: 5-12% revenue impact in bear case, 0-3% in base case.
[3] YouTube subscription revenue crosses $50B annual run rate (CNBC, 1 week ago)
    YouTube Premium and Music subscribers reach 125M globally.
    YouTube TV: 9M subs, raising prices 15% in March across all tiers.""",
}

FAKE_OPTIONS = {
    "NVDA": {"iv_30d": 38.2, "put_call_ratio": 0.72, "max_pain": 135.0,
             "gamma_exposure_b": 4.8, "skew_25d": -2.1,
             "atm_30d_call_premium": 7.85, "atm_30d_put_premium": 6.92,
             "open_interest_jan2025_140c": 184000},
    "MSFT": {"iv_30d": 24.6, "put_call_ratio": 0.91, "max_pain": 415.0,
             "gamma_exposure_b": 2.3, "skew_25d": -1.4,
             "atm_30d_call_premium": 9.20, "atm_30d_put_premium": 8.75,
             "open_interest_jan2025_420c": 96200},
    "GOOGL": {"iv_30d": 28.1, "put_call_ratio": 0.84, "max_pain": 175.0,
              "gamma_exposure_b": 1.6, "skew_25d": -1.8,
              "atm_30d_call_premium": 4.10, "atm_30d_put_premium": 3.85,
              "open_interest_jan2025_180c": 71300},
}


# --------------------------------------------------------------------------- #
# LLM helper - uses GMI Cloud if available, otherwise returns realistic fake  #
# responses tagged by the system-prompt prefix.                               #
# --------------------------------------------------------------------------- #

async def _llm_call(
    tag: str,
    system: str,
    user: str,
    max_tokens: int = 500,
    temperature: float = 0.3,
) -> str:
    """Make an LLM call via GMI Cloud, or return a tagged fake response.

    The `tag` argument selects a fake response from FAKE_RESPONSES when
    GMI_API_KEY is not set, the call fails, or the call times out. This
    guarantees the demo always produces a coherent trace at the LLM-call
    layer even fully offline.
    """
    fake = FAKE_RESPONSES.get(tag, '{"result": "demo mode"}')
    if not HAS_GMI_KEY:
        # Simulate realistic LLM latency so the dashboard timing looks correct.
        await asyncio.sleep((0.5 + random.random() * 0.3) * LATENCY_SCALE)
        return fake
    try:
        completion = await GMI_CLIENT.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return completion.choices[0].message.content or fake
    except Exception:
        # Degrade to fake response on network/auth/timeout - never let the
        # demo break because GMI is slow or unreachable.
        await asyncio.sleep(0.4 * LATENCY_SCALE)
        return fake


# --------------------------------------------------------------------------- #
# Tool nodes - external data sources (SEC, web, options chain)                #
# --------------------------------------------------------------------------- #

@lens.trace(name="sec-filing-parser", node_type="tool")
async def sec_filing_parser(ticker: str) -> str:
    """Fetches the most recent 10-K excerpt for a ticker.

    In production this would call the SEC EDGAR API. For the demo we return
    a hand-curated, realistic excerpt.

    Returns: ~3000-char text blob of selected 10-K passages.
    """
    await asyncio.sleep(0.3 * LATENCY_SCALE)  # simulate EDGAR latency
    return FAKE_SEC_FILINGS.get(ticker, f"(no filing data for {ticker})")


@lens.trace(name="web-search", node_type="tool")
async def web_search_tool(ticker: str, query: str = "latest news") -> str:
    """Searches the web for recent news about a ticker.

    In production this would call Tavily/Serper. Returns 3 headlines with
    short summaries.
    """
    await asyncio.sleep(0.2 * LATENCY_SCALE)  # simulate web search latency
    headlines = FAKE_NEWS.get(ticker, f"(no recent news for {ticker})")
    return f"Search query: {query}\n\n{headlines}"


@lens.trace(name="options-data-fetcher", node_type="tool")
async def options_data_fetcher(ticker: str) -> dict:
    """Fetches a snapshot of the options chain for a ticker.

    In production this would call CBOE/Polygon. Returns key derivatives
    metrics: implied vol, put/call ratio, max pain, gamma exposure, skew.
    """
    await asyncio.sleep(0.15 * LATENCY_SCALE)  # simulate market data API latency
    return FAKE_OPTIONS.get(ticker, {"error": f"no options data for {ticker}"})


@lens.trace(name="publisher", node_type="tool")
async def publisher(report: str) -> dict:
    """Publishes the final briefing to Slack / email distribution lists.

    For the demo, prints a Slack-style message to stdout.
    """
    await asyncio.sleep(0.1)
    print()
    print("=" * 64)
    print("📈  *Morning Briefing Posted to #pm-team*")
    print("=" * 64)
    preview = report.strip().splitlines()
    for line in preview[:6]:
        print(f"   {line}")
    if len(preview) > 6:
        print(f"   ... ({len(preview) - 6} more lines)")
    print("=" * 64)
    return {
        "channel": "#pm-team",
        "recipients": ["pm-team@hedgefund.example", "analysts@hedgefund.example"],
        "message_id": "slack-msg-" + str(random.randint(1000, 9999)),
        "delivered_at_utc": "2026-05-23T06:00:00Z",
    }


# --------------------------------------------------------------------------- #
# Agent nodes - each makes an LLM call via _llm_call().                       #
# --------------------------------------------------------------------------- #

TICKER_PLANNER_SYSTEM = (
    "You are a portfolio analyst. Extract exactly 3 stock tickers from the "
    "user's query and return them as a JSON object: "
    '{"tickers": ["...", "...", "..."]}. Return only the JSON object.'
)


@lens.trace(name="ticker-planner", node_type="agent")
async def ticker_planner(query: str) -> list[str]:
    """Decides which tickers to research based on the user query.

    Returns: list of 3 ticker strings, e.g. ["NVDA", "MSFT", "GOOGL"].
    """
    raw = await _llm_call(
        "ticker_planner", TICKER_PLANNER_SYSTEM, query, max_tokens=120)
    # Parse defensively: model may return JSON, raw list, or prose.
    try:
        parsed = json.loads(raw)
        tickers = parsed.get("tickers") if isinstance(parsed, dict) else parsed
    except Exception:
        tickers = None
    if not isinstance(tickers, list) or len(tickers) < 3:
        # Default basket for the demo.
        tickers = ["NVDA", "MSFT", "GOOGL"]
    return [str(t).upper() for t in tickers[:3]]


RESEARCHER_SYSTEM = (
    "You are a senior equity analyst. Given the following data sources for "
    "{ticker}, write a structured research brief as a single JSON object: "
    '{{"ticker": "...", "thesis": "...", '
    '"key_metrics": {{...}}, "risks": ["..."], '
    '"rating": "BUY|HOLD|SELL", "target_price": <float>}}. '
    "Return only the JSON object."
)


@lens.trace(name="researcher", node_type="agent")
async def researcher_agent(ticker: str, *, slow: bool = False) -> dict:
    """A single-ticker researcher that pulls data from multiple tools.

    Each researcher calls one or more tool nodes (SEC filing, web search,
    options data) and then asks an LLM to synthesize them into a brief.

    Returns: research-brief dict (see RESEARCHER_SYSTEM schema).
    """
    if slow:
        # Used by DEMO_MODE="timeout" to trigger an asyncio.TimeoutError.
        await asyncio.sleep(30.0)

    # → parallel tool fan-out (within this researcher)
    filing, news, options = await asyncio.gather(
        sec_filing_parser(ticker),
        web_search_tool(ticker, f"{ticker} latest financials"),
        options_data_fetcher(ticker),
    )

    user_prompt = (
        f"Ticker: {ticker}\n\n"
        f"=== 10-K excerpt ===\n{filing}\n\n"
        f"=== Recent news ===\n{news}\n\n"
        f"=== Options chain snapshot ===\n{json.dumps(options)}\n\n"
        "Write the research brief now."
    )
    raw = await _llm_call(
        f"researcher_{ticker}",
        RESEARCHER_SYSTEM.format(ticker=ticker),
        user_prompt,
        max_tokens=600,
    )
    try:
        brief = json.loads(raw)
        if not isinstance(brief, dict):
            raise ValueError("expected object")
    except Exception:
        # Fall back to the canned response for safety - never let a researcher
        # node fail in DEMO_MODE="success".
        brief = json.loads(FAKE_RESPONSES.get(
            f"researcher_{ticker}", FAKE_RESPONSES["researcher_NVDA"]))
    brief.setdefault("ticker", ticker)
    # Pad the brief with the full supporting data the analyst inspected.
    # This is realistic - production briefs carry their source evidence -
    # and it creates the context pressure that triggers FAILURE POINT A
    # at the synthesizer when 3 briefs are concatenated naively.
    brief["_supporting_data"] = {
        "filing_excerpt": filing,
        "news_summary": news,
        "options_snapshot": options,
        "analyst_notes": (
            f"Multi-source synthesis for {ticker} prepared {ticker}-research-"
            f"v2026.05.23. Reviewed 10-K excerpt, last 7 days of news flow, "
            f"and full options chain. Cross-checked against consensus sell-"
            f"side estimates from Bloomberg, ranked decile analyst pods, and "
            f"recent 13F filings. Conviction level: high. Time horizon: "
            f"6-12 months. Position sizing should reflect concentration "
            f"limits in the master fund's sector exposure budget. "
            f"Recommended hedge: long volatility via ATM straddles if IV "
            f"compresses below 25%. Risk-reward asymmetry favors upside "
            f"given the current market structure and the macro backdrop of "
            f"continued AI capex commitments through 2026. The thesis is "
            f"differentiated from sell-side consensus by emphasizing the "
            f"durability of the CUDA software moat (NVDA) and the AI "
            f"monetization curve (MSFT) over near-term capex anxiety."
        ) * 3,  # 3x repetition to reflect a thorough analyst writeup
    }
    return brief


SYNTHESIZER_SYSTEM = (
    "You are a portfolio strategist. Synthesize the equity research briefs "
    "into a unified portfolio view as a single JSON object: "
    '{"portfolio_thesis": "...", "top_pick": "TICKER", '
    '"risk_adjusted_return_pct": <float>, '
    '"allocation": {"TICKER1": <weight>, "TICKER2": <weight>, ...}}. '
    "Return only the JSON object."
)


# Per-brief char budget that the synthesizer can safely handle.
SYNTHESIZER_CONTEXT_LIMIT = 12_000


@lens.trace(name="synthesizer", node_type="agent")
async def synthesizer(research_briefs: list[dict]) -> dict:
    """Combines per-ticker briefs into a portfolio-level recommendation.

    FAILURE POINT A: when DEMO_MODE='broken', the synthesizer is given the
    full padded briefs (~6000 chars each) and the combined input exceeds the
    safe context window of 12,000 chars. It raises ValueError.
    """
    # Serialize the briefs as JSON.
    combined = json.dumps(research_briefs, indent=2)
    mode = _effective_mode()

    # # FAILURE POINT A - context_overflow check
    if mode == "broken" and len(combined) > SYNTHESIZER_CONTEXT_LIMIT:
        raise ValueError(
            f"Context window exceeded: synthesizer received {len(combined)} "
            f"characters of combined research data, but the safe processing "
            f"limit is {SYNTHESIZER_CONTEXT_LIMIT}. Three parallel research "
            f"agents each returned ~{len(combined) // 3} character briefs "
            f"that were concatenated without truncation."
        )

    # If the partial-fix path is taken (DEMO_MODE != "broken"), truncate.
    if len(combined) > SYNTHESIZER_CONTEXT_LIMIT:
        combined = combined[:SYNTHESIZER_CONTEXT_LIMIT] + "...[truncated]"

    raw = await _llm_call(
        "synthesizer", SYNTHESIZER_SYSTEM, combined, max_tokens=500)
    try:
        return json.loads(raw)
    except Exception:
        return json.loads(FAKE_RESPONSES["synthesizer"])


RISK_CHECKER_SYSTEM = (
    "You are a risk manager. Assess portfolio-level risk from the synthesis "
    "report. Return a single JSON object: "
    '{"var_95_pct": <float>, "max_drawdown_pct": <float>, '
    '"concentration_risk": "...", "tail_risk_flags": ["..."], '
    '"overall_risk_score": <1-10>}. Return only the JSON object.'
)


@lens.trace(name="risk-checker", node_type="agent")
async def risk_checker(synthesis: dict) -> dict:
    """Portfolio-level risk assessment.

    FAILURE POINT B: when DEMO_MODE='bad_json', simulate a model that returns
    truncated JSON; json.loads raises JSONDecodeError.
    """
    user = json.dumps(synthesis)
    raw = await _llm_call(
        "risk_checker", RISK_CHECKER_SYSTEM, user, max_tokens=400)

    # # FAILURE POINT B - bad_json (only reachable via DEMO_MODE="bad_json")
    if _effective_mode() == "bad_json":
        # Simulate model returning a truncated JSON string.
        truncated = '{"var_95_pct": 6.8, "max_drawdown_pct":'
        raise json.JSONDecodeError(
            "Model returned malformed JSON (response was truncated "
            "mid-object, likely due to max_tokens limit)",
            doc=truncated, pos=len(truncated),
        )

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(FAKE_RESPONSES["risk_checker"])


REPORT_WRITER_SYSTEM = (
    "You are a financial writer. Write a 3-paragraph executive summary of "
    "the portfolio recommendation for a hedge fund morning briefing. Be "
    "specific with numbers and conviction. Use plain prose, not JSON."
)


@lens.trace(name="report-writer", node_type="agent")
async def report_writer(synthesis: dict, risk: dict) -> str:
    """Writes the human-readable executive summary for the morning briefing."""
    user = (
        f"Portfolio synthesis:\n{json.dumps(synthesis, indent=2)}\n\n"
        f"Risk report:\n{json.dumps(risk, indent=2)}\n\n"
        "Write the briefing now."
    )
    raw = await _llm_call(
        "report_writer", REPORT_WRITER_SYSTEM, user, max_tokens=600,
        temperature=0.4)
    return raw.strip() or FAKE_RESPONSES["report_writer"]


# --------------------------------------------------------------------------- #
# Orchestrator - the root agent for the pipeline.                             #
# --------------------------------------------------------------------------- #

@lens.trace(name="financial-research-orchestrator", node_type="agent")
async def orchestrator(query: str) -> dict:
    """Main entry point for the nightly financial research pipeline.

    Args:
        query: Natural language description of what to research.
               Example: "Research AI infrastructure plays for our growth portfolio"

    Returns:
        Status dict with the final report and publish receipt.
    """
    mode = _effective_mode()
    print("\n🔍 Starting financial research pipeline...")
    print(f"   Query: {query}")
    print(f"   Mode:  {mode}{' (fix applied)' if mode == 'success' and _is_fix_applied() else ''}\n")

    # Step 1: decide which tickers to research.
    tickers = await ticker_planner(query)
    print(f"   📋 Tickers selected: {tickers}")

    # Step 2: → parallel execution of 3 researchers (each pulls its own tools).
    print(f"   🔄 Researching {len(tickers)} tickers in parallel...")
    if mode == "timeout":
        try:
            research_results = await asyncio.wait_for(
                asyncio.gather(
                    researcher_agent(tickers[0]),
                    researcher_agent(tickers[1], slow=True),  # will time out
                    researcher_agent(tickers[2]),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                "researcher-1 exceeded 5s deadline (in DEMO_MODE=timeout)")
    else:
        research_results = await asyncio.gather(
            researcher_agent(tickers[0]),
            researcher_agent(tickers[1]),
            researcher_agent(tickers[2]),
        )

    # Step 3: synthesize. # FAILURE POINT A triggers here in DEMO_MODE=broken.
    print("   🧩 Synthesizing portfolio view...")
    synthesis = await synthesizer(research_results)

    # Step 4: risk check.
    print("   ⚠️  Assessing risk...")
    risk_report = await risk_checker(synthesis)

    # Step 5: write executive summary.
    print("   ✍️  Writing executive summary...")
    report = await report_writer(synthesis, risk_report)

    # Step 6: publish to Slack/email distribution lists.
    published = await publisher(report)

    return {
        "status": "complete",
        "tickers": tickers,
        "synthesis": synthesis,
        "risk_report": risk_report,
        "report": report,
        "published": published,
    }


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    # CLI args: anything after a leading "--" is treated as a query.
    # By default we run continuously every ~5 seconds, which is what the
    # autopsy dashboard demo expects. Pass --once to run a single iteration.
    argv = list(sys.argv[1:])
    run_once = False
    if "--once" in argv:
        argv.remove("--once")
        run_once = True
    query = (" ".join(argv) or
             "Research AI infrastructure plays for our growth portfolio")

    loop_delay_s = float(os.environ.get("AUTOPSY_LOOP_DELAY_S", "8"))

    print("=" * 60)
    print("  autopsy - Financial Research Pipeline Demo")
    print("=" * 60)
    print(f"  DEMO_MODE: {DEMO_MODE}")
    print(f"  GMI API:   {'configured' if HAS_GMI_KEY else 'not set (using fake responses)'}")
    print(f"  Model:     {MODEL if HAS_GMI_KEY else '(fake)'}")
    print(f"  Loop:      {'one-shot' if run_once else f'every {loop_delay_s:.0f}s'}")
    print(f"  Fix marker (home):  {FIX_MARKER_PATH}")
    print(f"  Fix marker (cwd):   {FIX_MARKER_PATH_LOCAL}")
    print("=" * 60)

    def _run_one() -> str:
        """Run the pipeline once. Returns status string for the loop log."""
        try:
            result = asyncio.run(orchestrator(query))
            preview = result.get("report", "")[:160].replace("\n", " ")
            print("\n✅ Pipeline completed successfully")
            print(f"   Report preview: {preview}...")
            return "success"
        except Exception as e:
            print(f"\n❌ Pipeline failed: {type(e).__name__}: {e}")
            print("   → Open the autopsy dashboard to diagnose and click "
                  "'Apply fix & replay'.")
            return f"error:{type(e).__name__}"

    if run_once:
        _run_one()
        sys.exit(0)

    iteration = 0
    try:
        while True:
            iteration += 1
            mode_now = _effective_mode()
            print()
            print("━" * 60)
            print(f"  iteration #{iteration}  |  effective mode = {mode_now}"
                  + ("  ✅ FIX APPLIED" if _is_fix_applied() else ""))
            print("━" * 60)
            _run_one()
            print(f"\n💤 Sleeping {loop_delay_s:.0f}s before next iteration "
                  f"(Ctrl+C to stop)...")
            try:
                # Allow the user time to inspect the trace on the dashboard
                # and click "Apply fix & replay".
                import time
                time.sleep(loop_delay_s)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass

    print("\n👋 Loop stopped.")
    print("   Dashboard: http://localhost:7823")
