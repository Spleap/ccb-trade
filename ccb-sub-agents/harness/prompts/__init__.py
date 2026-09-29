"""提示词层：L1~L5 五层模板与上下文渲染（ARCHITECTURE §6）。"""
from harness.prompts.system import (L3_RULES, L4_FLOW, L5_CONTRACT,  # noqa: F401
                                     build_system_prompt, context_message)

__all__ = ["L3_RULES", "L4_FLOW", "L5_CONTRACT", "build_system_prompt", "context_message"]
