"""跨函数可变状态：model_available 熔断、UNDERSTANDING 理解缓存。属性式读写（state.x），勿拷贝。"""

model_available = True  # 一旦连接层失败则置 False，后续直接走规则兜底
UNDERSTANDING = None  # dict | None
