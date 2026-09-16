# NJUPTBadminton

南邮羽毛球预约接入开发项目，当前不是完成品。

## 当前已验证

- 本机捕获启动器：Clash 7897 上游、仅南邮域名、临时代理快照与恢复。
- 本地凭据只存 private/，不提交 Git；CA 安装见 SESSION_GUIDE.md。
- 2026-09-16 通过本机凭据真实读取羽毛球类型、当天三牌楼场次。
- live_probe.py 为只读场次查询/有界观测入口，最多30次、间隔3秒。

## Windows 使用

当前复用参考仓库的 uv 环境，也可在本目录用 `uv venv` 和 `uv pip install -r requirements.txt` 建立自己的环境。
`start_session.bat` 用于捕获、检查及恢复；注意其解释器路径目前是本机固定路径，其他电脑需要调整。
`python live_probe.py --date 2026-09-16` 只读查询。
`python live_probe.py --date 2026-09-16 --observe 20` 有界观测。
日期请替换为实际需要的日期。只有已捕获且服务端接受的凭据才能读取。

## 尚未完成

- 完整订单查询、状态核验、多目标状态机和交互式正式预约。
- 中午真实放场窗口观测；非放场时间的可用快照不能证明放场时间。
- 异常恢复、代理启动的完整实机测试。

core.py/cli.py/start_booking.bat 是早期原型，不能当成正式抢场入口。
HTTP Date 只有秒级，可能来自网关；不能宣称毫秒对时。客户端 RTT 包含代理、网络与服务端等开销。
不自动支付，不无限重试，不绕过限流。不承诺抢到。

参考实现：FinalOath/NJUPT_badminton_booking。仅作接口研究依据，其真实接口兼容性仍需逐项验证。
