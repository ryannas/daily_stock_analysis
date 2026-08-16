# -*- coding: utf-8 -*-
"""
===================================
DSA 系统健康检查脚本
===================================

逐节点测试 daily_stock_analysis 各环节能否跑通，避免反复用真实
分析任务试错浪费 LLM token。

默认只运行「非消耗性」检查（不调 LLM、不发送通知、不实搜）：
    python scripts/dsa_healthcheck.py

可选消耗性测试（显式开启才会执行）：
    python scripts/dsa_healthcheck.py --search   # 真实搜索一次新闻（不耗 LLM token）
    python scripts/dsa_healthcheck.py --llm      # 调用一次 LLM（消耗少量 token）
    python scripts/dsa_healthcheck.py --notify   # 实际发送一条测试通知
    python scripts/dsa_healthcheck.py --all      # 以上全部

其他控制：
    --stocks 600519,300750   指定用于测试的股票代码（默认取 STOCK_LIST 第一只）
    --config                 只看配置
    --proxy                  只看代理检测
    --db                     只看数据库
    --fetch                  只看数据源逐源测试
"""
import os
# 与 main.py 保持一致的代理引导：USE_PROXY=true 且非 GitHub Actions 时才注入代理环境变量。
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

import argparse
import logging
import sys
import time
import urllib.request
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

warnings.filterwarnings("ignore")  # 抑制 pytdx/sqlite 等第三方 ResourceWarning 噪音


def configure_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        for kwargs in ({"encoding": "utf-8", "errors": "replace"}, {"errors": "replace"}):
            try:
                reconfigure(**kwargs)
                return
            except Exception:
                continue


logging.basicConfig(level=logging.CRITICAL)  # 压掉第三方库噪音，只看脚本输出
logger = logging.getLogger("dsa_healthcheck")

# 国内数据源域名（受系统代理影响、一般不需要代理直连）
DOMESTIC_DATA_DOMAINS = [
    "eastmoney.com", "push2his.eastmoney.com", "sina.com.cn",
    "hq.sinajs.cn", "baostock.com", "qq.com", "gtimg.cn",
    "tushare.pro", "tendata.cn", "10jqka.com.cn",
]

PASS, FAIL, SKIP = "✓", "✗", "-"


def print_header(title: str):
    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)


def print_result(name: str, ok: bool, detail: str = "", indent: int = 2):
    mark = PASS if ok else FAIL
    pad = " " * indent
    print(f"{pad}{mark} {name}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 配置
def check_config():
    print_header("1. 配置加载")
    from src.config import get_config
    config = get_config()

    print(f"  股票列表: {', '.join(config.stock_list) if config.stock_list else '(空!)'}")
    print(f"  数据库路径: {config.database_path}")
    print(f"  SCREENING_ENABLED: {config.screening_enabled}")

    print_section("LLM 渠道")
    channels = getattr(config, "llm_channels", None) or []
    if not channels:
        print_result("LLM_CHANNELS", False, "未配置任何可用 LLM 渠道（分析将失败）")
    for ch in channels:
        enabled = ch.get("enabled", True)
        models = ch.get("models") or []
        keys = ch.get("api_keys") or []
        detail = (
            f"protocol={ch.get('protocol')} surface={ch.get('api_surface')} "
            f"models={','.join(models)} keys={len(keys)}"
        )
        print_result(f"渠道 {ch.get('name')}", bool(enabled and models and keys), detail)

    print_section("通知渠道")
    from src.notification import NotificationService, ChannelDetector
    try:
        svc = NotificationService()
        channels_avail = svc.get_available_channels()
        if channels_avail:
            names = [ChannelDetector.get_channel_name(c) for c in channels_avail]
            print(f"  已配置: {', '.join(names)}")
        else:
            print_result("通知渠道", False, "未配置任何通知渠道（仅本地保存报告）")
    except Exception as exc:
        print_result("通知服务初始化", False, str(exc))

    print_section("搜索源")
    from src.search_service import get_search_service
    try:
        search = get_search_service()
        print_result("SearchService", search.is_available, f"providers={len(getattr(search, '_providers', []))}")
        for p in getattr(search, "_providers", []):
            print_result(f"  {getattr(p, 'name', type(p).__name__)}", bool(p.is_available))
        if not search.is_available:
            print("     提示: 可配置 TAVILY_API_KEYS / BRAVE_API_KEYS / SEARXNG_BASE_URLS 等任一搜索源")
    except Exception as exc:
        print_result("SearchService", False, str(exc))
    return True


# ---------------------------------------------------------------- 2. 代理检测
def _read_windows_system_proxy():
    """Read Windows system proxy from registry (fallback for urllib)."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if enabled and server:
                return f"http://{server}"
    except Exception:
        pass
    return None


def check_proxy():
    print_header("2. 代理检测（国内数据源影响排查）")
    # 环境变量代理（含 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY）
    env_proxies = {
        k.upper(): v for k, v in os.environ.items()
        if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") and v.strip()
    }
    sys_proxy = _read_windows_system_proxy()

    if not env_proxies and not sys_proxy:
        print_result("未检测到系统/环境代理", True, "国内数据源应可直连")
        return True

    if sys_proxy:
        print(f"  · Windows 系统代理 = {sys_proxy}")
        print_result("系统代理", False,
                     "已开启系统代理，国内数据源(eastmoney/sina等)可能被强制走代理而失败")
        print("     处理: ① Clash 给 eastmoney.com/sina.com.cn 等加 DIRECT 规则；")
        print("            ② 跑 DSA 前关闭系统代理；③ 设置 NO_PROXY 覆盖后重启 WebUI。")
    for k, v in env_proxies.items():
        print(f"  · 环境变量 {k} = {v}")
    if not sys_proxy and env_proxies:
        print("  ⚠ 存在环境变量代理，注意国内数据源是否受影响（用 NO_PROXY 排除国内域名）。")
    return not sys_proxy


# ---------------------------------------------------------------- 3. 数据库
def check_db():
    print_header("3. 数据库")
    from src.storage import get_db
    from sqlalchemy import text
    db = get_db()
    session = db.get_session()
    try:
        result = session.execute(text("""
            SELECT code, COUNT(*) as cnt, MIN(date), MAX(date), data_source
            FROM stock_daily
            GROUP BY code
            ORDER BY code
        """))
        rows = result.fetchall()
        if rows:
            print(f"  已有 {len(rows)} 只股票数据:")
            for r in rows:
                print(f"    · {r[0]:<10} {r[1]:<6}条 {r[2]} ~ {r[3]}  {r[4] or ''}")
        else:
            print_result("stock_daily 数据", True, "表存在但暂无数据（正常）")
    except Exception as exc:
        print_result("数据库查询", False, str(exc))
    finally:
        session.close()
    return True


# ---------------------------------------------------------------- 4. 数据源逐源测试
def _call_with_timeout(fn, timeout_seconds: float):
    import concurrent.futures
    import threading

    result_holder = {}
    stop = threading.Event()

    def _run():
        warnings.filterwarnings("ignore")
        try:
            result_holder["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            result_holder["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_seconds)
    if t.is_alive():
        # 超时：后台线程继续运行（daemon），不阻塞脚本退出
        stop.set()
        return None, f"超时({timeout_seconds:.0f}s)"
    if "error" in result_holder:
        return None, str(result_holder["error"])[:200]
    return result_holder.get("value"), None


def check_fetch(stock_codes):
    print_header("4. 数据源逐源测试")
    from data_provider import DataFetcherManager

    manager = DataFetcherManager()
    # 逐个源独立测试（不走 manager 的自动切换）
    fetchers = getattr(manager, "_fetchers", None)
    if not fetchers:
        # 触发懒加载
        _ = manager.available_fetchers
        fetchers = getattr(manager, "_fetchers", [])

    print(f"  可用数据源: {', '.join(f.name for f in fetchers)}")
    # 主源集合：东财系 + TickFlow（配置了 TICKFLOW_API_KEY 时视为稳定主源）
    primary_sources = {"EfinanceFetcher", "AkshareFetcher"}
    if os.getenv("TICKFLOW_API_KEY"):
        primary_sources.add("TickFlowFetcher")
    all_ok = True
    for code in stock_codes:
        print(f"\n  --- 测试股票 {code} ---")
        ok_sources = set()
        for f in fetchers:
            t0 = time.time()
            df, err = _call_with_timeout(lambda c=code, f=f: f.get_daily_data(c, days=5), timeout_seconds=25)
            elapsed = time.time() - t0
            if err:
                # 短错误摘要
                err_short = err.splitlines()[0][:150] if err else ""
                print_result(f"{f.name}", False, f"{err_short} ({elapsed:.1f}s)")
            elif df is None or df.empty:
                print_result(f"{f.name}", False, f"返回空数据 ({elapsed:.1f}s)")
            else:
                rows = len(df)
                last_date = str(df.iloc[-1].get("date", ""))[:10]
                ok_sources.add(f.name)
                print_result(f"{f.name}", True, f"{rows} 行, 最新 {last_date} ({elapsed:.1f}s)")
        if not (ok_sources & primary_sources):
            all_ok = False
            print(f"    ⚠ 主源(Efinance/Akshare/TickFlow)全部失败，仅靠 fallback 源兜底，"
                  f"数据时效与字段可能不完整")
    print("\n  注: 部分数据源对特定市场（如 Yfinance 对 A 股）可能预期不支持，属正常，"
          "关键看主源(Efinance/Akshare/TickFlow)是否成功。")
    if not all_ok:
        print("  结论: 存在主源全失败的股票，建议检查网络/代理后重试，或启用"
              "ENABLE_EASTMONEY_PATCH=true / 配置 TICKFLOW_API_KEY。")
    return all_ok


# ---------------------------------------------------------------- 5. 搜索源
def check_search(stock_codes, do_real_search: bool):
    print_header("5. 搜索源")
    from src.search_service import get_search_service
    search = get_search_service()
    if not search.is_available:
        print_result("搜索服务", False, "无可用搜索源")
        return False

    for p in getattr(search, "_providers", []):
        print_result(f"{getattr(p, 'name', type(p).__name__)}", bool(p.is_available))

    if not do_real_search:
        print("\n  提示: 使用 --search 可对第一只股票做一次真实新闻搜索验证。")
        return True

    code = stock_codes[0]
    from src.storage import get_db
    from sqlalchemy import text
    db = get_db()
    session = db.get_session()
    name = ""
    try:
        row = session.execute(text(
            "SELECT name FROM stock_daily WHERE code=:code LIMIT 1"
        ), {"code": code}).first()
        if row:
            name = row[0] or ""
    except Exception:
        pass
    finally:
        session.close()
    if not name:
        name = code

    print(f"\n  对 {name}({code}) 执行一次真实新闻搜索 ...")
    t0 = time.time()
    try:
        resp = search.search_stock_news(code, name, max_results=3)
        elapsed = time.time() - t0
        if resp.success and resp.results:
            print_result("search_stock_news", True,
                         f"{len(resp.results)} 条, provider={resp.provider}, 耗时 {elapsed:.1f}s")
            for r in resp.results[:3]:
                print(f"      · {r.title[:60]}")
        else:
            print_result("search_stock_news", False,
                         f"provider={resp.provider}, error={resp.error_message or '无结果'} ({elapsed:.1f}s)")
    except Exception as exc:
        print_result("search_stock_news", False, str(exc)[:200])
    return True


# ---------------------------------------------------------------- 6. 通知
def check_notification(do_send: bool):
    print_header("6. 通知渠道")
    from src.notification import NotificationService, ChannelDetector
    svc = NotificationService()
    channels = svc.get_available_channels()
    if not channels:
        print_result("通知渠道", False, "未配置任何通知渠道")
        return False
    names = [ChannelDetector.get_channel_name(c) for c in channels]
    print(f"  已配置: {', '.join(names)}")

    if not do_send:
        print("\n  提示: 使用 --notify 会向上述渠道发送一条测试消息。")
        return True

    test_msg = (
        f"## 🧪 DSA 健康检查测试\n\n"
        f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 目的: 验证通知渠道配置\n\n"
        f"收到此消息说明通知链路正常 ✓"
    )
    print("\n  发送测试消息到所有渠道 ...")
    result = svc.send_with_results(test_msg, email_send_to_all=True)
    if result.channel_results:
        for cr in result.channel_results:
            print_result(f"{cr.channel}", bool(cr.success),
                         f"{(cr.error_code or cr.diagnostics or '')[:120]}")
    ok = result.success
    print_result("通知发送整体", bool(ok), result.status)
    return bool(ok)


# ---------------------------------------------------------------- 7. LLM
def check_llm():
    print_header("7. LLM 调用测试（消耗少量 token）")
    from src.config import get_config
    config = get_config()
    channels = getattr(config, "llm_channels", None) or []
    channels = [c for c in channels if c.get("enabled", True) and c.get("models") and c.get("api_keys")]
    if not channels:
        print_result("LLM 渠道", False, "没有可用的已启用渠道")
        return False

    import litellm
    ch = channels[0]
    model = ch["models"][0]
    api_key = ch["api_keys"][0]
    base_url = ch.get("base_url") or None
    api_surface = ch.get("api_surface", "chat_completions")

    print(f"  渠道: {ch['name']}  模型: {model}  surface: {api_surface}")
    if base_url:
        print(f"  base_url: {base_url}")

    kwargs = {
        "model": model,
        "api_key": api_key,
        "messages": [{"role": "user", "content": "请只回复两个字：正常"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    if base_url:
        kwargs["api_base"] = base_url

    t0 = time.time()
    try:
        response = litellm.completion(**kwargs)
        elapsed = time.time() - t0
        content = ""
        try:
            content = response.choices[0].message.content or ""
        except Exception:
            pass
        print_result(f"LLM 调用 {model}", True, f"返回: {content!r} ({elapsed:.1f}s)")
        return True
    except Exception as exc:
        elapsed = time.time() - t0
        msg = str(exc)
        hint = ""
        low = msg.lower()
        if "api key" in low or "401" in msg:
            hint = "；API Key 可能无效"
        elif "model" in low and "not found" in low:
            hint = "；模型名可能不正确"
        elif "connect" in low or "timeout" in low or "proxy" in low:
            hint = "；网络不通或走了不可达代理"
        print_result(f"LLM 调用 {model}", False, f"{msg[:160]}{hint} ({elapsed:.1f}s)")
        return False


def print_section(title: str):
    print(f"\n--- {title} ---")


def main():
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="DSA 系统健康检查", add_help=True)
    parser.add_argument("--all", action="store_true", help="运行所有测试（含 LLM/搜索/通知实发）")
    parser.add_argument("--config", action="store_true", help="仅配置检查")
    parser.add_argument("--proxy", action="store_true", help="仅代理检测")
    parser.add_argument("--db", action="store_true", help="仅数据库")
    parser.add_argument("--fetch", action="store_true", help="仅数据源逐源测试")
    parser.add_argument("--search", action="store_true", help="含真实搜索（默认只列可用性）")
    parser.add_argument("--llm", action="store_true", help="真实调用一次 LLM")
    parser.add_argument("--notify", action="store_true", help="实际发送测试通知")
    parser.add_argument("--stocks", type=str, default="", help="测试股票，逗号分隔")
    args = parser.parse_args()

    from src.config import get_config
    config = get_config()
    stock_codes = [s.strip() for s in args.stocks.split(",") if s.strip()] or config.stock_list
    if not stock_codes:
        print("STOCK_LIST 未配置，且未通过 --stocks 指定股票。")
        return 1
    stock_codes = stock_codes[:5]  # 避免全市场慢测

    explicit = [args.config, args.proxy, args.db, args.fetch]
    want_consuming = args.all or args.llm or args.notify or args.search

    run_sections = []
    if args.all or not any(explicit):
        run_sections = ["config", "proxy", "db", "fetch", "search"]
    else:
        if args.config:
            run_sections.append("config")
        if args.proxy:
            run_sections.append("proxy")
        if args.db:
            run_sections.append("db")
        if args.fetch:
            run_sections.append("fetch")

    print("\n" + "🚀" * 24)
    print("  DSA 系统健康检查  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("🚀" * 24)

    results = []
    if "config" in run_sections:
        results.append(("配置", check_config()))
    if "proxy" in run_sections:
        results.append(("代理", check_proxy()))
    if "db" in run_sections:
        results.append(("数据库", check_db()))
    if "fetch" in run_sections:
        results.append(("数据源", check_fetch(stock_codes)))
    if "search" in run_sections:
        results.append(("搜索", check_search(stock_codes, do_real_search=args.all or args.search)))
    if args.all or args.llm:
        results.append(("LLM", check_llm()))
    if args.all or args.notify:
        results.append(("通知", check_notification(do_send=True)))

    print_header("结果汇总")
    for name, ok in results:
        print_result(name, bool(ok))
    failed = [n for n, ok in results if not ok]
    print()
    if failed:
        print(f"  存在问题的节点: {', '.join(failed)}")
        print("  提示: 结合各节点详情定位；代理节点告警重点看系统代理是否拦截国内数据源。")
    else:
        print("  全部节点正常 ✓")

    if not want_consuming and not explicit:
        print("\n  说明: 未执行 LLM/真实搜索/通知发送（避免消耗 token / 打扰）。")
        print("  如需完整验证: python scripts/dsa_healthcheck.py --all")

    return 0 if not failed else 1


if __name__ == "__main__":
    # 提前抑制警告，避免 pytdx/ssl/sqlite 的 ResourceWarning 噪音
    warnings.filterwarnings("ignore")
    warnings.simplefilter("ignore", ResourceWarning)
    os.environ["PYTHONWARNINGS"] = "ignore"
    sys.exit(main())
