# 展品运输邮件的原件采集与入场交接

本项目包含两部分:

- `source/`:隔离的本地来源服务(合成邮箱 + 入场交接台),只响应自身接口,不代替采集程序判断。
- `collector/`:业务采集程序。通过真实 IMAP(只读)领取邮件原件,按原件内容归到运输批次,
  向本地入场交接台提交有凭据的到货资料,并留下可查的缺件、扣留原因与待核实项。

所有姓名、联系方式、证件、费用和机构均为演练数据,不连接实际业务系统。

## 环境和运行

使用 Python 3.11,只用标准库。准备环境时执行 `python3 -m pip install -r requirements.txt`。

1. 启动来源:`python3 -m source.app --port 8765`(IMAP 默认 1143,可用 `--mail-port` 改)。
   详细入口、字段及演练阶段见 [来源契约](docs/source-contract.md)。服务仅监听回环地址;
   重启后来源回到第一阶段、模拟接收记录清空,采集程序自有状态在 `--data-dir` 中另行保管。
2. 采集一轮:`python3 -m collector --data-dir var collect`
   (默认连接 127.0.0.1:1143 与 http://127.0.0.1:8765,可用 `--imap-port`、`--desk` 等覆盖)。
3. 查询(全部脱敏,不泄漏合成联系人):
   - `status` 各批次箱状态;`missing` 缺件及待核清单;`detained` 已到货扣留箱(交收货组处置)
   - `stops` 停交通知及现场确认;`handoffs` 交接记录与回执
   - `messages` 已领取原件;`explain <哈希前缀>` 解释某封邮件被归到哪里
   - `review` 待人工核实;`review-resolve <编号> [--choose 内容哈希前缀]` 人工了结

采集程序的设计、状态口径与演练阶段验证步骤见 [采集程序说明](docs/collector.md)。

## 检查

执行 `python3 -m unittest discover -s tests -v` 检查来源边界与采集业务(真实 IMAP 会话、
UIDVALIDITY 重建、归档移动、扣留推翻放行、断线重连不重复交接等),执行
`python3 -m compileall -q source collector tests` 检查 Python 源码。
