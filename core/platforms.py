"""插件能备份哪些平台：一处定义，两处消费。

`metadata.yaml` 的 `support_platforms` 是给人看的能力声明，`main.py` 挑平台时用的是
同一份名单。分开放两份就会漂：面板上写着「支持 Telegram」，实际备份 0 条、状态
`failed`，用户不知道是平台不支持还是插件坏了。

所以这里的判据只有一条——**有没有回捞历史消息的接口**。

AstrBot 的适配器只做「收消息、转事件」，历史消息得靠平台自己的 API，而适配器
很少把它透出来。目前为止只有 `aiocqhttp` 会：它的 OneBot 客户端（NapCat /
Lagrange / go-cqhttp 都走这个适配器）的 `call_action` 能直接打
`get_group_msg_history` / `get_group_member_list` / `get_group_info`，
备份引擎的 `PlatformAdapter` 正是按这几个名字探测的。

其他适配器（telegram / discord / slack / wecom / qq_official …）拿不到历史消息，
但**实时归档仍然可用**：消息进群即写库走的是事件钩子，与平台无关。那条界线写在
README「支持平台」一节——能力声明要经得起对照。
"""

#: 真能回捞历史消息、成员名单、群信息的平台适配器名（= 宿主 ADAPTER_NAME_2_TYPE 的 key）
HISTORY_PLATFORMS = ("aiocqhttp",)

#: 有客户端、但没历史接口的适配器。只在「没得选」时优先于 webchat 之类：
#: 它们的 `get_client()` 能拿到东西，值得先试一次，失败也会如实报 failed。
FALLBACK_ORDER = ("telegram", "discord", "slack", "wecom", "qq_official")

#: 挑平台时的偏好顺序，长在前短在后
PREFERRED_ORDER = HISTORY_PLATFORMS + FALLBACK_ORDER
