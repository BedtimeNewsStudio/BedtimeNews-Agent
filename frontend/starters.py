"""
Sample-question data for the BedtimeNews knowledge base.

Questions grouped by category, each drawn from the 正文 of episodes that
actually exist in BedtimeNews-Transcripts, across all five programs (睡前消息 /
参考信息 / 高见 / 讲点黑话 / 产经破壁机). Grounding them in real episodes is the
point: a suggested question the archive cannot answer is worse than no
suggestion, since it sends a first-time visitor straight into an empty result.

The frontend shows one randomly chosen question per category, under the
category name as a heading on desktop (mobile drops the headings for density);
"换一批" redraws. This module is plain data with no UI-framework dependency; the
server exposes it as JSON at /api/starters.
"""

from typing import TypedDict


class Topic(TypedDict):
    question: str  # sent to the agent and shown as the button text


class Category(TypedDict):
    name: str  # Chinese category name, shown as the group heading on desktop
    topics: list[Topic]


CATEGORIES: list[Category] = [
    {
        "name": "地方财政与城市",
        "topics": [
            {
                "question": "柳州为什么会成为城投债“四大网红”之一？轻轨为什么烂尾？",
            },
            {
                "question": "独山县是怎么烧掉400亿的？钱具体花在了哪些项目上？",
            },
            {
                "question": "土地财政跳水之后，地方政府靠什么填补收入缺口？",
            },
            {
                "question": "信达支援贵州地方债是怎么回事？为什么说2023年酷似1999年？",
            },
            {
                "question": "城投平台转型成“产投”是真转型还是换个马甲？",
            },
            {
                "question": "收缩型城市名单意味着什么？人口流失的城市该怎么办？",
            },
            {
                "question": "茅台和贵州地方债之间是什么关系？地方财政对它的依赖有多深？",
            },
            {
                "question": "土地财政之后，为什么说要向香港地铁的模式学习？",
            },
            {
                "question": "本溪这类城市为什么编制比例偏高？和物流成本有什么关系？",
            },
        ],
    },
    {
        "name": "人口与社会",
        "topics": [
            {
                "question": "社会化抚养到底是什么主张？为什么争议这么大？",
            },
            {
                "question": "中央生育补贴为什么必须和地方反着来？",
            },
            {
                "question": "讨论人口问题的三个层次分别是什么？",
            },
            {
                "question": "所谓“生育奇迹”是怎么被发现造假的？",
            },
            {
                "question": "内蒙人口第一县为什么要启动逆城市化？",
            },
            {
                "question": "日本搬出东京就给钱，为什么还是拉不起生育率？",
            },
            {
                "question": "人口下降如何一步步冲击幼儿园和中小学？",
            },
            {
                "question": "天价彩礼屡禁不止，背后是怎样的婚育结构问题？",
            },
        ],
    },
    {
        "name": "教育与就业",
        "topics": [
            {
                "question": "衡水模式到底是什么？为什么屡屡引发争议？",
            },
            {
                "question": "“山河四省”这个说法反映了什么样的高考不平等？",
            },
            {
                "question": "“山河大学”为什么只靠民间热情建不起来？",
            },
            {
                "question": "高考平权为什么说广东可能是突破口？",
            },
            {
                "question": "考研人数下降、考公人数上涨，说明了什么？",
            },
            {
                "question": "校招需求萎缩到什么程度了？年轻人的出路在哪里？",
            },
            {
                "question": "校园预制菜为什么让家长这么不放心？",
            },
            {
                "question": "哪些专业开始招不满了？高校专业设置出了什么问题？",
            },
        ],
    },
    {
        "name": "科技与航天",
        "topics": [
            {
                "question": "星舰第十二次试飞意味着什么？重型运载火箭到什么阶段了？",
            },
            {
                "question": "美国的阿尔忒弥斯登月计划为什么一再拖延？",
            },
            {
                "question": "朱雀三号首飞和蓝箭的回收成功，对中国航天意味着什么？",
            },
            {
                "question": "为什么说民营航天“给机会不中用”？",
            },
            {
                "question": "中国航天和美国的差距到底有多大？差在哪里？",
            },
            {
                "question": "AI热潮下的算力投资会不会形成泡沫？",
            },
            {
                "question": "AI芯片产业在突围，为什么散户反而留在山顶？",
            },
            {
                "question": "美国封杀之下，国内芯片项目为什么还会自己垮掉？",
            },
        ],
    },
    {
        "name": "国际政治与地缘",
        "topics": [
            {
                "question": "叙利亚复兴党六十年兴亡史说明了什么？",
            },
            {
                "question": "“乌克兰没有五千亿”是什么意思？俄乌战争的账该怎么算？",
            },
            {
                "question": "特朗普再次上台后的人事布局和“F计划”是怎么回事？",
            },
            {
                "question": "马斯克当官意味着什么？他和特朗普政府的关系如何演变？",
            },
            {
                "question": "尹锡悦的三小时政变是怎么发生又怎么失败的？",
            },
            {
                "question": "琉球王国是怎么被日本一步步吞并的？",
            },
            {
                "question": "安倍时代的日本派阀政治是怎么运作又怎么终结的？",
            },
            {
                "question": "新一轮关税战对中国出口和全球产业链有什么影响？",
            },
        ],
    },
    {
        "name": "产业与消费",
        "topics": [
            {
                "question": "金价狂飙、排队疯抢，我们到底在买黄金还是买奢侈品？",
            },
            {
                "question": "卫龙统一辣条市场之后，麻辣王子靠什么逆袭？",
            },
            {
                "question": "资本热潮退去后，为什么是江西人称霸了烘焙行业？",
            },
            {
                "question": "回转寿司为什么能从“没人吃”变成“排队王”？",
            },
            {
                "question": "被苹果抛弃的“果链”企业是怎么转向华为求生的？",
            },
            {
                "question": "快招加盟的诈骗套路是怎么运作的？为什么总有人上当？",
            },
            {
                "question": "小米YU7创造了什么样的工业奇迹？后面的挑战是什么？",
            },
            {
                "question": "V社一条百字更新，为什么能带崩二十亿美元的饰品市场？",
            },
        ],
    },
    {
        "name": "工程与安全",
        "topics": [
            {
                "question": "香港宏福苑火灾的事故经过和原因是什么？",
            },
            {
                "question": "为什么说“每一场火灾，原因都在十年前”？老房子的隐患在哪？",
            },
            {
                "question": "江苏响水月港大桥系杆拱梁塌落事故是怎么发生的？",
            },
            {
                "question": "包钢1·18爆炸事故的关键信息有哪些？",
            },
            {
                "question": "四川资中震群和页岩气开采有没有关系？",
            },
            {
                "question": "从唐山、汶川到临汾，中国人还能不能放心用预制板？",
            },
            {
                "question": "景区吊桥这类设施的安全管理为什么屡屡出问题？",
            },
            {
                "question": "地震到底能不能预测？预警系统能做到什么程度？",
            },
        ],
    },
    {
        "name": "医疗与监管",
        "topics": [
            {
                "question": "药品集采是怎么把药价打下来的？对行业意味着什么？",
            },
            {
                "question": "为什么会接连有中成药被停用？审批标准发生了什么变化？",
            },
            {
                "question": "医保门诊统筹改革为什么让一部分人觉得吃亏了？",
            },
            {
                "question": "“宇宙最大医院”关闭西院区说明了什么？",
            },
            {
                "question": "甲醛保鲜大白菜、敌敌畏消毒餐厅这类事件暴露了什么监管漏洞？",
            },
            {
                "question": "西贝“货不对板”争议里，预制化到底该不该背锅？",
            },
            {
                "question": "医药反腐风暴是怎么回事？带金销售为什么屡禁不止？",
            },
            {
                "question": "“劳动法律意识过强”这种说法反映了怎样的劳资现实？",
            },
        ],
    },
]
