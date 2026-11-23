"""Video replies focus on content; processing statistics stay in internal notes."""
import re

VIDEO_REPLY_POLICY='\n视频回复默认直接讲内容、观点和结论，不主动报告视频总时长、分段数量、抽样帧数、覆盖范围、下载状态、从头到尾处理了或不是逐帧观看等工作过程。不要以“看完全片了”“全片多久”“抽了几张画面”开头。只有用户明确询问处理方法、时长或抽帧统计时才回答这些问题。用户要求按时间段梳理时可保留内容时间戳；确实缺失的音频、失败区间和不确定事实仍须简短说明。不能把视频内容里的帧率与系统抽样数量混为一谈。'
VIDEO_EMPTY_REPLY='视频已读取，但这次没有生成有效的内容摘要。'


def asks_processing(text):
    return bool(re.search(r'你.{0,8}(?:怎么|如何).{0,8}(?:看|分析|处理)|(?:视频|全片).{0,5}(?:多长|多久|时长)|(?:时长|抽帧|帧数|抽样画面).{0,8}(?:多少|数量|几|方法|流程)|抽了多少|看了几帧',str(text or '')))


def process_clause(text):
    value=text.strip()
    if re.search(r'失败|缺失|没听清|听不清|无法|不能确认|未能|未读到|不确定',value):return False
    if re.fullmatch(r'(?:不是|并非)逐帧(?:看|观看|分析|看完)?',value):return True
    if re.search(r'^(?:处理|覆盖)范围|^全片时间轴.{0,12}(?:分段|处理)',value):return True
    if re.search(r'^(?:全程|从头到尾).{0,10}(?:处理|看完)|^(?:全片|全程|本次).{0,8}(?:没有缺段|无缺段|没有缺失)|^(?:没有缺段|无缺段)',value):return True
    if re.search(r'^(?:我|本鲸|这个视频|这条视频|视频).{0,16}(?:看了全片|看完了全片|已经下载|已下载)',value):return True
    if re.search(r'^(?:共|一共|有|取得|基于|根据|依据|抽了|抽取了?|采样了?|提取了?|全片.{0,8})?\s*[0-9一二两三四五六七八九十百]+(?:张|幅|个)?(?:抽样画面|采样画面|关键帧|抽样帧)',value):return True
    if re.search(r'^(?:我|本鲸)?(?:抽了|抽取了?|采样了?|提取了?)\s*[0-9一二两三四五六七八九十百]+(?:张|幅|个)?帧',value):return True
    if re.search(r'^(?:分成?|共|一共)\s*[0-9一二两三四五六七八九十百]+段(?:处理|解析|分析)?$',value):return True
    if re.search(r'后|之前|之后|开始|接着|结尾',value):return False
    return bool(re.search(r'^(?:实际视频总长|视频总长|视频全长|视频时长|全片|全长|总时长|时长|大概|大约|约)(?:大约|大概|约为|约|为|有|共)?\s*(?:[0-9一二两三四五六七八九十百]+(?:[.:：][0-9]+)?)\s*(?:小时|分钟|秒钟|分|秒|[：:][0-9]+)',value))


def content_only(text,question=''):
    if not isinstance(text,str) or asks_processing(question):return text
    chunks=re.split(r'([。！？!?\n])',text);output=[]
    for index in range(0,len(chunks),2):
        sentence=chunks[index];ending=chunks[index+1] if index+1<len(chunks) else ''
        clauses=re.split(r'[，,；;]',sentence)
        marked=[process_clause(clause) for clause in clauses]
        if any(marked):
            kept=[]
            for clause,drop in zip(clauses,marked):
                clause=clause.strip()
                intro=(re.fullmatch(r'(?:主人|本鲸|我)[呀啊呢]?',clause) or re.fullmatch(r'.{0,32}(?:我|本鲸)?看(?:完了|过了?|了全片)',clause))
                if clause and not drop and not intro:kept.append(clause)
            sentence='，'.join(kept)
        if sentence.strip():output.append(sentence+ending)
        elif ending=='\n':output.append('\n')
    return re.sub(r'\n{3,}','\n\n',''.join(output)).strip()
