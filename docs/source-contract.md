# 来源和当前接口

## 原始响应

fixtures/sample.json含source_id、url、status、headers、body_base64和可选metadata，headers为有序二元列表，保留重复字段；body_base64解码成真实原始实体字节，原件也存在fixtures/source-body.bin。
app.source.from_packet校验状态、头与字节编码，生成只读Response。header(name)不区分字段名大小写并按顺序逗号连接重复值，headers原元组始终保留。
sha256只证明当前实体字节，不证明来源业务身份或采集完整。fetch执行一次HTTP请求，保留错误响应，不自动跳转，不替来源解释缓存、分页或业务错误。
fetch(url,source_id,method,headers,body,max_bytes)保留方法和请求体，读取超过预算即失败；适配器现有超时为5秒。app.service.collect_one调用该来源接口，inspect(packet)供已有响应读取，二者输出source_id、response_sha256和summary。
实际访问资格由上层来源配置控制，本地来源服务只绑定127.0.0.1；其/source路径返回指定响应，其他路径404。服务器记录请求方法、路径、头与body_base64；现有记录只用于合成来源联调，不包含真实凭据。

## 业务来源口径

采集窗为UTC半开区间，实例与窗口有交集才进入现行结果，变化记录保留原键(source_id,UID,原始RECURRENCE-ID)。浮动时间用来源预登记时区，DATE按当地全天解释。支持RFC5545的DAILY/WEEKLY/MONTHLY、INTERVAL、COUNT/UNTIL、BYDAY/BYMONTHDAY/BYMONTH/BYSETPOS、WKST及RDATE/EXDATE；其余规则显式未支持。TZID用来源VTIMEZONE或已冻结IANA映射，二者冲突报告；不发送邀请或修改来源。新响应是该source完整日历版本，重复UID/实例冲突不得凭文件顺序裁决。

## 当前能力与配置

当前app.baseline.analyze只产生一份响应的概览，兼容入口继续保留；新采集可增加入口，不能把字节摘要等同于协议或业务完整性。
metadata={}。读取预算、授权范围和来源凭据属于调用配置，真实密钥不放在资料目录或交付证据中。
