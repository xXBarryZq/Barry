"""Read-only WindPy connection check. Run beside a logged-in Wind terminal.

Does not install software, request credentials, send orders or transmit a report.
"""

import importlib
import math
import platform
import struct
import sys
from datetime import datetime
from pathlib import Path


def main():
    lines = []

    def say(message):
        lines.append(message)
        print(message, flush=True)

    def finish(code, summary):
        say("")
        say("检查结果：" + summary)
        say("只需将上面这一行结果发给开发者，不需要发送账号、密码或验证码。")
        report = Path(__file__).with_name("Wind检查结果-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".txt")
        try:
            with report.open("x", encoding="utf-8-sig") as output:
                output.write("\n".join(lines) + "\n")
            print("已在当前文件夹保存检查结果。", flush=True)
        except OSError:
            print("当前文件夹无法保存文本，请直接查看窗口中的结果。", flush=True)
        return code

    say("Barry · Wind 接口检查")
    say("此工具仅检查连接和读取一条 ETF 报价，不会下单或上传检查结果。")
    say("当前 Python：" + platform.python_version() + " / " + str(struct.calcsize("P") * 8) + " 位")
    if sys.platform != "win32":
        return finish(2, "W01：请在已安装 Wind 客户端的 Windows 电脑上运行。")
    try:
        api = importlib.import_module("WindPy").w
    except ImportError:
        return finish(3, "W02：当前 Python 找不到 WindPy。请向 Wind 客服确认 Python 接口安装方式和兼容版本；此结果不能说明账号没有权限。")
    except Exception:
        return finish(3, "W03：WindPy 加载失败。请向 Wind 客服确认 Python 版本、位数和接口组件是否匹配。")

    started = False
    try:
        say("正在连接本机 Wind，请保持 Wind 客户端打开并已登录……")
        result = api.start()
        started = True
        error = getattr(result, "ErrorCode", None)
        if error != 0:
            number = str(error) if isinstance(error, int) else "未知"
            return finish(4, "W04：Wind 接口启动失败，错误码 " + number + "。请确认客户端已登录，并向 Wind 客服确认接口是否启用。")
        if not api.isconnected():
            return finish(4, "W05：Wind 接口未连接。请先打开并登录 Wind 客户端。")
        say("接口已连接，正在读取沪深300 ETF（510300.SH）的一条报价……")
        result = api.wsq("510300.SH", "rt_last")
        error = getattr(result, "ErrorCode", None)
        if error != 0:
            number = str(error) if isinstance(error, int) else "未知"
            return finish(5, "W06：接口已连接，但 ETF 报价读取失败，错误码 " + number + "。请确认该品种的数据权限。")
        try:
            price = float(result.Data[0][0])
        except (AttributeError, IndexError, TypeError, ValueError):
            price = float("nan")
        if not math.isfinite(price) or price <= 0:
            return finish(5, "W07：接口已连接，但暂未取得有效 ETF 报价；需要继续核对品种、市场状态和权限。")
        return finish(0, "W00：接口可连接，已读取一条 ETF 报价。尚未验证实时延迟、分钟 K 线或国内期货权限。")
    except Exception:
        return finish(6, "W08：Wind 接口运行异常。请向 Wind 客服确认客户端、Python 和接口组件是否兼容。")
    finally:
        if started:
            try:
                api.stop()
            except Exception:
                pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
