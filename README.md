# 园区交流日历的重复事件与例外实例采集

现有组件读取一份来源响应，输出当前概览及字节摘要；采集层在此基础上按UTC半开窗交付实际活动实例。
app/source.py保留响应状态、重复头和实体原字节，一次请求不自动跟随重定向；app/baseline.py解释当前单响应，app/service.py将概览绑定到来源身份及摘要。
app/icalparse.py解析内容行、类型化取值与VTIMEZONE；app/rrule.py在本地墙钟空间有界展开重复规则；app/series.py按UID归组并应用RRULE/RDATE/EXDATE、RECURRENCE-ID例外与THISANDFUTURE调期/取消；app/collect.py换算UTC、按窗口归类（含调出窗口的shadow记录）、标注未能展开的系列，并按来源版本去重与比对。
app/lab.py提供回环HTTP来源，支持GET、POST、HEAD、PROPFIND和REPORT，记录收到的请求，用于在可控来源上联调。fixtures为合成资料，不连接真实企业或个人。
来源协议、字段身份、实例口径与当前边界见[来源契约](docs/source-contract.md)。Python 3.14，现有代码仅使用标准库。

读取当前响应概览：

    python -m app.service fixtures/sample.json

按UTC半开窗领取实例（--floating-tz为来源预登记的浮动/全天时区，--state落盘交付状态）：

    python -m app.collect fixtures/sample.json 2026-01-01T00:00:00Z 2026-02-01T00:00:00Z --floating-tz Asia/Shanghai --state /tmp/collect-state.json

运行现有模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests

如需单独启动本地来源，运行下列命令后读取打印的回环地址；结束时按Ctrl+C：

    python -m app.lab fixtures/sample.json
