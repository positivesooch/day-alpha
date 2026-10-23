"""Stocks by sector, and commodities, for the window's market list.

US common stocks in each group are the liquid names of that type. They trade
on Alpaca and on a MetaTrader 5 stock-CFD account. Commodity groups list the
ETFs a stock broker can buy (GLD for gold, USO for oil) and, separately, the
futures Yahoo charts and the names most MetaTrader 5 brokers use (XAUUSD,
USOIL). Alpaca does not list those futures; the window adds them only for the
sim and for MetaTrader 5.

The full US common-stock list is not in this file. `scan.py universe` downloads
it from Nasdaq Trader. The window's "All US common stocks" button does that.
"""

# id, kind, name, symbols (stocks or commodity ETFs), futures, mt5 names for those futures
GROUPS = [
    {"id": "index", "kind": "stock", "name": "Index ETFs",
     "symbols": ["SPY", "QQQ", "IWM", "DIA", "VTI"]},
    {"id": "tech", "kind": "stock", "name": "Technology",
     "symbols": ["AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ORCL", "CRM", "ADBE", "CSCO", "IBM", "INTC", "AMAT", "QCOM",
                 "TXN", "NOW"]},
    {"id": "communication", "kind": "stock", "name": "Communication",
     "symbols": ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T"]},
    {"id": "discretionary", "kind": "stock", "name": "Consumer discretionary",
     "symbols": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "BKNG", "TJX", "CMG"]},
    {"id": "staples", "kind": "stock", "name": "Consumer staples",
     "symbols": ["WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "CL", "KHC", "TGT"]},
    {"id": "health", "kind": "stock", "name": "Healthcare",
     "symbols": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR", "PFE", "AMGN"]},
    {"id": "financials", "kind": "stock", "name": "Financials",
     "symbols": ["BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "C", "SCHW"]},
    {"id": "industrials", "kind": "stock", "name": "Industrials",
     "symbols": ["CAT", "GE", "RTX", "HON", "UNP", "BA", "DE", "UPS", "LMT", "ETN"]},
    {"id": "energy", "kind": "stock", "name": "Energy stocks",
     "symbols": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "WMB", "OXY", "VLO"]},
    {"id": "materials", "kind": "stock", "name": "Materials",
     "symbols": ["LIN", "SHW", "FCX", "NEM", "APD", "ECL", "NUE", "DOW", "DD", "MLM"]},
    {"id": "utilities", "kind": "stock", "name": "Utilities",
     "symbols": ["NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC", "XEL", "ED"]},
    {"id": "realestate", "kind": "stock", "name": "Real estate",
     "symbols": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "AVB"]},
    {"id": "metals", "kind": "commodity", "name": "Metals",
     "symbols": ["GLD", "SLV", "PPLT", "PALL", "CPER"],
     "futures": ["GC=F", "SI=F", "PL=F", "PA=F", "HG=F"],
     "mt5": {"GC=F": "XAUUSD", "SI=F": "XAGUSD", "PL=F": "XPTUSD", "PA=F": "XPDUSD", "HG=F": "XCUUSD"}},
    {"id": "energy-commodity", "kind": "commodity", "name": "Energy commodities",
     "symbols": ["USO", "UNG", "BNO", "UGA"],
     "futures": ["CL=F", "BZ=F", "NG=F"],
     "mt5": {"CL=F": "USOIL", "BZ=F": "UKOIL", "NG=F": "NATGAS"}},
    {"id": "agriculture", "kind": "commodity", "name": "Agriculture",
     "symbols": ["CORN", "WEAT", "SOYB", "CANE", "JO", "NIB", "BAL"],
     "futures": ["ZC=F", "ZW=F", "ZS=F", "SB=F", "KC=F", "CT=F", "CC=F"],
     "mt5": {"ZC=F": "CORN", "ZW=F": "WHEAT", "ZS=F": "SOYBEAN", "SB=F": "SUGAR", "KC=F": "COFFEE",
             "CT=F": "COTTON", "CC=F": "COCOA"}},
    {"id": "livestock", "kind": "commodity", "name": "Livestock",
     "symbols": ["COW"],
     "futures": ["LE=F", "HE=F"],
     "mt5": {"LE=F": "CATTLE", "HE=F": "HOGS"}},
    {"id": "commodity-baskets", "kind": "commodity", "name": "Commodity baskets",
     "symbols": ["DBC", "DBA", "GSG"]},
]


def by_id(group_id):
    for g in GROUPS:
        if g["id"] == group_id:
            return g
    raise KeyError(group_id)


def ids_of(kind=None):
    return [g["id"] for g in GROUPS if kind is None or g["kind"] == kind]


def pick(group_ids, futures=False):
    """Symbols to watch, stocks and commodity ETFs first, futures after them when asked."""
    chosen = []
    seen = set()

    def add(sym):
        sym = sym.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            chosen.append(sym)

    for gid in group_ids:
        g = by_id(gid)
        for sym in g["symbols"]:
            add(sym)
        if futures:
            for sym in g.get("futures") or []:
                add(sym)
    return chosen


def mt5_map(group_ids):
    """Futures ticker -> the MetaTrader 5 name, for the groups that were picked."""
    out = {}
    for gid in group_ids:
        out.update(by_id(gid).get("mt5") or {})
    return {k.upper(): v for k, v in out.items()}
