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
            {
                "question": "深圳救不救万科，为什么说是一道经济学例题？",
            },
            {
                "question": "“一删了之”式化债和化债不力分别是怎么回事？",
            },
            {
                "question": "五百万买百强县是怎么回事？“摇摆县”的榜单生意是什么？",
            },
            {
                "question": "北京亦庄为什么被称为中国地方产业资本的最高形态？",
            },
            {
                "question": "海南封关之后，自贸港还能被“再抢救一次”吗？",
            },
            {
                "question": "珠海国资反腐风暴是怎么回事？",
            },
            {
                "question": "棚改五年后又收购老破小，广州这样做说明了什么？",
            },
            {
                "question": "政府欠企业的钱为什么要还？欠款报告为什么又不公开？",
            },
            {
                "question": "“湖北湖南东北化”指的是什么现象？",
            },
            {
                "question": "正威集团的巨额营业收入和各地工厂的实际生产情况，为什么会对不上？",
            },
            {
                "question": "鹤岗人口持续减少，建设新机场的客流预测和投资回报该怎么算？",
            },
            {
                "question": "保交楼为什么离不开房企融资？销售低迷怎样传导到竣工进度？",
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
            {
                "question": "2025年只出生了792万人，这个数字意味着什么？",
            },
            {
                "question": "育儿补贴为什么会和“反内卷”挂钩？",
            },
            {
                "question": "灵活就业超过两亿人，劳动权益面临哪些问题？",
            },
            {
                "question": "“规模性返乡滞乡”是什么现象？",
            },
            {
                "question": "公粮视同社保为什么引发这么大的讨论？",
            },
            {
                "question": "为什么北京的年轻人数量减半了？",
            },
            {
                "question": "提高农民养老金为什么成了两会的热门话题？",
            },
            {
                "question": "“全球Z世代革命”和海贼王旗是怎么回事？",
            },
            {
                "question": "网瘾学校的暴力为什么说只是武校的零头？",
            },
            {
                "question": "农村留守儿童到底有多少？不同统计口径为什么会得出差距很大的数字？",
            },
            {
                "question": "户籍制度改革如何影响人口流动和城乡公共服务？节目谈过哪些障碍？",
            },
            {
                "question": "职工养老保险与城乡居民养老保险的待遇差距，为什么值得关注？",
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
            {
                "question": "名校停招学硕、“博士比驴多”，研究生教育出了什么问题？",
            },
            {
                "question": "高考人数为什么连续两年下降？",
            },
            {
                "question": "禁止重点班、快慢班，现实中为什么很难落实？",
            },
            {
                "question": "国考弃考率为什么这么高？",
            },
            {
                "question": "考公考编学院和“三支一扶”缩招说明了什么？",
            },
            {
                "question": "嵊泗全员直升高中是怎么回事？",
            },
            {
                "question": "十二年一贯制学校是什么？为什么会被推广？",
            },
            {
                "question": "博士大量扩招和产业升级之间是什么关系？",
            },
            {
                "question": "“无工作社会”和“新一代潘晓之问”讨论的是什么？",
            },
            {
                "question": "中考后的普职分流为什么引发争议？德国职业教育的经验能否照搬？",
            },
            {
                "question": "AI替代初级岗位，会怎样影响年轻人的入行机会和人才培养？",
            },
            {
                "question": "节目讨论过哪些劳动仲裁案例？劳动者维权为什么常常遇到阻力？",
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
            {
                "question": "轨道空间碎片到底有多大影响？",
            },
            {
                "question": "星链工厂为什么被称为卫星产业的“福特时刻”？",
            },
            {
                "question": "AI漫剧和AI短剧看着爆火，为什么说真正赚钱的只有一类人？",
            },
            {
                "question": "低价Token中转站看似血亏，背后有哪些收割套路？",
            },
            {
                "question": "OpenAI估值千亿，为什么还处于烧钱不盈利的状态？",
            },
            {
                "question": "存储芯片是怎么从七年巨亏做到一季回本的？",
            },
            {
                "question": "被AI影响的战争有什么变化？",
            },
            {
                "question": "韩国政府网络瘫痪，为什么说风险来自二十年前？",
            },
            {
                "question": "中国氦气是怎么突围的？",
            },
            {
                "question": "武汉弘芯的芯片项目是怎么运作的？为什么风险还会传到济南？",
            },
            {
                "question": "天龙三号试车时为什么会意外升空？事故暴露了哪些工程问题？",
            },
            {
                "question": "星链从造火箭发展到批量造卫星，产业化的关键变化有哪些？",
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
            {
                "question": "南非为什么可能是最先被非洲移民问题炸掉的国家？",
            },
            {
                "question": "伊朗、古巴、委内瑞拉为什么被说成是“一家”？",
            },
            {
                "question": "高市早苗组阁说明日本自民党进入了怎样的历史阶段？",
            },
            {
                "question": "比亚迪巴西奴工事件是怎么回事？中国该怎么应对？",
            },
            {
                "question": "爪哇岛高铁为什么亏损？高铁出海能去哪里？",
            },
            {
                "question": "纽约社会主义市长是怎么当选的？他要怎么对付封建地主？",
            },
            {
                "question": "李嘉诚卖英国电网，法国为什么很高兴？",
            },
            {
                "question": "美国是怎么没收柬埔寨电诈集团150亿美元比特币的？",
            },
            {
                "question": "中印关系为什么出现了解冻？",
            },
            {
                "question": "苏联解体前，中国媒体如何评价戈尔巴乔夫？这种评价经历了哪些变化？",
            },
            {
                "question": "“一带一路”的项目和投资规模，为什么很难用一个总数说明？",
            },
            {
                "question": "琉球的本土认同、日本统治和美军基地之间，有怎样的历史关系？",
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
            {
                "question": "手机越卖越贵，厂商为什么反而越赚越少？",
            },
            {
                "question": "车企为什么宁愿贴息也不肯直接降价？",
            },
            {
                "question": "快递涨价一毛五，这笔钱最后谁来扛？",
            },
            {
                "question": "泡泡玛特从收藏品变成快消品，还能留住消费者吗？",
            },
            {
                "question": "库迪放弃9块9，咖啡行业为什么告别了低价战？",
            },
            {
                "question": "现制酸奶为什么只火了两年？",
            },
            {
                "question": "宜家中国为什么一口气关闭了7家门店？",
            },
            {
                "question": "煤电占比首次跌破50%，电费会涨吗？",
            },
            {
                "question": "中国轮胎从产能出海到产品出海，难在哪里？",
            },
            {
                "question": "演唱会票价越来越贵，为什么依旧一票难求？",
            },
            {
                "question": "法国为什么要围剿SHEIN？",
            },
            {
                "question": "光伏产业为什么反复出现产能过剩？补贴和出口退税在其中起了什么作用？",
            },
            {
                "question": "欧盟电池碳足迹要求，为什么会影响宁德时代等企业获取绿电？",
            },
            {
                "question": "网约车平台降低抽成，能否缓解司机在运力饱和下的收入压力？",
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
            {
                "question": "秦岭鳌太线和深圳七娘山户外死亡事件暴露了什么问题？",
            },
            {
                "question": "宜宾48天发生34次地震是怎么回事？",
            },
            {
                "question": "武汉一个村62人患癌，调查发现了什么？",
            },
            {
                "question": "高标准农田为什么会变成摆设？",
            },
            {
                "question": "河南庄稼泡水、广西村屯内涝，暴雨防灾的短板在哪里？",
            },
            {
                "question": "溯源纸尿裤甲酰胺事件是怎么回事？",
            },
            {
                "question": "地下储能项目是什么？它安全吗？",
            },
            {
                "question": "电动车新规为什么要让车主交钱修路？",
            },
            {
                "question": "引江济淮工程为什么被称为安徽的“小南水北调”？它兼顾了哪些用途？",
            },
            {
                "question": "北煤南运为什么既要修重载铁路，又要发展特高压输电？",
            },
            {
                "question": "毛虾为什么能逼停核电站？海水冷却系统面临哪些风险？",
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
            {
                "question": "集采药从印度采购，为什么说有差距要承认？",
            },
            {
                "question": "医保还没到缺钱的时候，为什么就卡住了？",
            },
            {
                "question": "治愈乙肝的药，为什么跨国企业未必能赚钱？",
            },
            {
                "question": "删掉“尚不明确”“速效”这类表述，为什么救不了中成药？",
            },
            {
                "question": "老年保健品是怎么骗走退休金的？",
            },
            {
                "question": "“神医”孙晓楠被卧底调查，发现了什么？",
            },
            {
                "question": "门诊报销设置每日限额是怎么回事？",
            },
            {
                "question": "哪些原研药退出了市场？为什么？",
            },
            {
                "question": "微塑料太多还是你太胖？",
            },
            {
                "question": "减肥药赛道为什么突然卷了起来？",
            },
            {
                "question": "健康税为什么针对烟草、酒精和含糖饮料？公共卫生与财政目标如何兼顾？",
            },
            {
                "question": "高价儿童近视防控镜片的宣传，为什么不能代替临床证据？",
            },
            {
                "question": "老年保健品会销如何把产品、讲师和健康馆连成一套销售体系？",
            },
        ],
    },
]

# Search starters are single keywords, named entities or natural phrases,
# not bags of loosely related words. They cover recurring cases and long-run
# themes found in transcript bodies and topic headings. The placeholder draws
# from the full library; chips sample six. Chinese phrases are segmented by
# the existing search backend, not treated as exact-phrase constraints.
SEARCH_STARTERS: list[str] = [
    "地方债务",
    "房地产税",
    "人口老龄化",
    "高考",
    "药品集采",
    "延迟退休",
    "光刻机",
    "贸易战",
    "人工智能",
    "台积电",
    "俄乌战争",
    "粮食安全",
    "城市更新",
    "统计造假",
    "生育率",
    "独山县",
    "正威集团",
    "鹤岗",
    "恒大",
    "万科",
    "柳州轻轨",
    "武汉弘芯",
    "社会化抚养",
    "土地财政",
    "烂尾楼",
    "职业教育",
    "衡水中学",
    "山河大学",
    "生育补贴",
    "留守儿童",
    "户籍制度",
    "外卖骑手",
    "劳动仲裁",
    "失业保险",
    "星舰",
    "星链",
    "可回收火箭",
    "阿尔忒弥斯",
    "半导体",
    "大基金",
    "氦气",
    "琉球",
    "雅万高铁",
    "叙利亚",
    "苏联解体",
    "一带一路",
    "核电",
    "南水北调",
    "特高压",
    "页岩气",
    "高标准农田",
    "宁德时代",
    "光伏",
    "网约车",
    "快递",
    "中成药",
    "鸿茅药酒",
    "保健品",
    "健康税",
    "消费税",
    "戒网瘾学校",
]
