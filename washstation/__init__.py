"""雪具洗护站用水与交接核算包（仅依赖 Python 标准库）。

模块划分：

- ``core``       区间值、数据仓储、通用状态
- ``meters``     累计水表差分、换表/缺测/迟报冲突识别
- ``water``      分时段、柜台、料筐类别的水量归属、估算与核验
- ``quota``      经营日/时段水额与占用
- ``planning``   合洗约定、机器/料筐/水额约束与次日排程情景
- ``quality``    检测单版本订正与推翻的级联重判
- ``tanks``      回用罐、混合等级与预留充足性
- ``transfers``  跨站调拨生命周期与两端计量差
- ``billing``    物业用量单/柜台计量单版本、分摊与争议
- ``reconcile``  柜台回执、水量报表与批次的四方核对
- ``commitments`` 租赁可发量承诺、低等级放行证据与退回重洗替换
- ``shift``      下一班交接、异常归因与取件承诺追踪
- ``station``    对外门面 ``Station``
- ``report``     JSON / Markdown 汇总报告
"""

from .core import Range, Repository, load_repository
from .station import Station

__all__ = ["Range", "Repository", "load_repository", "Station"]
