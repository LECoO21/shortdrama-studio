#!/usr/bin/env python3
"""Default: local connection/probe status only, with no inference or model GET.

--run-text / --run-frames prepare the authorized smoke path. Before using either,
the user must confirm in ChatGPT settings that additional credits are disabled.
--plan-only-confirmed records that confirmation; it grants no new authorization
and cannot verify or change billing settings. Do not run it without that consent.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.chatgpt_auth import ChatGPTAuthError, ChatGPTAuthManager  # noqa: E402
from app.chatgpt_probe import ChatGPTProbe, ChatGPTProbeError  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list-models", action="store_true", help="读取当前账号模型目录，不推理")
    action.add_argument("--run-text", action="store_true", help="固定一句话连接测试，无工具")
    action.add_argument("--run-frames", action="store_true", help="仅指定素材首个/末个镜头候选的缩略图，并非原片首尾精确帧；无工具")
    parser.add_argument("--model", help="可选；默认使用当前账号目录第一个可见模型，保持服务排序")
    parser.add_argument("--request-id", help="8–64 位稳定请求编号；重复运行相同编号不会再次发起请求")
    parser.add_argument("--plan-only-confirmed", action="store_true", help="仅在已确认关闭额外 credits 后使用；不产生付费授权")
    parser.add_argument("--retry-unknown-from", metavar="OLD_ID", help="仅限已确证本地缺头拒绝的未知请求；须先获得用户明确允许一次重复套餐消耗。此标志本身不产生授权，原记录不改写，每个原请求最多一次。")
    args = parser.parse_args(argv)
    if (args.list_models or args.run_text or args.run_frames) and not args.request_id:
        parser.error("外部请求必须提供 --request-id，以保留去重与未知结果记录")
    if args.request_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", args.request_id):
        parser.error("--request-id 需为 8–64 位字母、数字、下划线或短横线")
    if (args.run_text or args.run_frames) and not args.plan_only_confirmed:
        parser.error("先由用户在 ChatGPT 设置核对并确认关闭额外 credits；确认后才可使用 --plan-only-confirmed")
    if args.retry_unknown_from is not None and (not (args.run_text or args.run_frames)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", args.retry_unknown_from)
            or args.retry_unknown_from == args.request_id):
        parser.error("--retry-unknown-from 仅用于显式文本/抽帧运行，且需有效原编号和不同的新请求编号")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        with ChatGPTAuthManager() as auth, ChatGPTProbe(auth) as probe:
            if args.list_models:
                result = probe.models(request_id=args.request_id)
            elif args.run_text or args.run_frames:
                # Account-specific discovery is part of this explicit smoke only.
                # Default status never discovers models or accesses a bearer.
                catalog = probe.status()["catalog"]
                if not catalog or not 0 <= time.time() - catalog["fetched_at"] <= 1800:
                    catalog_id = "catalog_" + hashlib.sha256(args.request_id.encode()).hexdigest()[:32]
                    discovered = probe.models(request_id=catalog_id)
                    if discovered["state"] != "completed":
                        print(json.dumps(discovered, ensure_ascii=False))
                        return 1
                operation = probe.frames if args.run_frames else probe.text
                result = operation(model=args.model, request_id=args.request_id,
                                   plan_only_confirmed=True, retry_unknown_from=args.retry_unknown_from)
            else:
                status = auth.status()
                result = {"connection": {key: status.get(key) for key in
                          ("connected", "plan_usage_enabled", "needs_login")}, "probe": probe.status(),
                          "message": "仅读取本机状态；没有发送素材、读取模型目录或调用推理。"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if (result.get("state", "completed") == "completed"
                         and result.get("result", {}).get("smoke_passed") is not False) else 1
    except (ChatGPTAuthError, ChatGPTProbeError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "interrupted", "message": "已中断；已登记请求结果可能未知，不会自动重提。"}}, ensure_ascii=False))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
