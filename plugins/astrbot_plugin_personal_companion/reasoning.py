"""Per-event DeepSeek effort selection; never mutate a shared provider instance."""
import re

HIGH_PROVIDER='deepseek-rolechat-high'
MAX_PROVIDER='deepseek-rolechat-max'

def effort_for(event):
    text=str(event.message_str or '').strip()
    if re.fullmatch(r'(?:你好|早上好|晚上好|晚安|你是谁|你叫什么|你在干嘛|你在干什么|吃了吗|睡了吗|我的昵称是什么|这个群叫什么)[呀啊呢吗嘛吧啦～~？?。！!\s]*',text):
        return 'high'
    raw=event.message_obj.raw_message
    if raw.get('quote') or raw.get('link'):
        return 'max'
    if re.search(r'[？?]|什么|怎么|为何|为什么|能否|是否|请问|解释|介绍|总结|分析|比较|评价|判断|证明|计算|帮我|帮忙|查一下|联网|搜索|概括|解读|读一下|这篇|文章|论文|代码|报错|原理|如何|区别|原因|求解|建议',text,re.I):
        return 'max'
    return 'high'

def select_effort(event,context):
    effort=effort_for(event)
    provider=MAX_PROVIDER if effort=='max' else HIGH_PROVIDER
    if context.get_provider_by_id(provider) is None:
        return None
    event.set_extra('selected_provider',provider)
    event.set_extra('companion_reasoning_effort',effort)
    return effort
