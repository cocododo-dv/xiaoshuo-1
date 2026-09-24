# Style Reference 黄金测试语料

本目录承载 Style Reference v1.1 抽取/验证流水线的端到端黄金测试。
参见《风格参考模块重构执行手册 v1.1》§9.2。

这些黄金用例验证摄取、统计、分类、抄袭检测和确定性回归，不单独证明“同一情节
能否复现不同作者风格”。RAG v2 已取消“原段前缀召回原段”的词面指标，改用冻结
的“异题材同风格 vs 同题材异风格”合成 A/B 验证检索机制；它仍只是诊断证据，
真正的风格贴合度结论必须来自隐藏语料与真人跨内容盲测基准。

## 目录结构

```
backend/tests/golden/style_reference/
├── corpus/
│   ├── luxun_short_stories.txt   # 主力测试集:鲁迅短篇 11 篇,约 66,000 字
│   ├── zhuziqing_essays.txt      # 对照测试集:朱自清散文 4 篇,约 8,300 字
│   ├── zhuziqing_benchmark_essays.txt # 跨内容基准:朱自清散文 16 篇,约 43,400 字
│   └── luxun_kongyiji.txt        # 下限测试集:单篇《孔乙己》,约 2,600 字(全层 skip)
├── expected/
│   ├── luxun_ingest_expected.json      # 主力集 ingest 期望(段数 / input_assessment / 段型分布)
│   └── zhuziqing_ingest_expected.json  # 对照集 ingest 期望
└── README.md
```

## 语料来源与版权

全部为**公有领域**作品,2026-06-13 取自中文维基文库(zh.wikisource.org,
REST API `page/html` + zh-hans 变体转换,HTMLParser 提取 <p> 正文并滤除
许可证/导航噪声):

- **鲁迅**(1881–1936,卒逾 50/70 年,两岸与美国均已进入公有领域;
  所收各篇均发表于 1930 年以前):《狂人日记》《孔乙己》《药》《明天》
  《一件小事》《头发的故事》《风波》《故乡》《阿Q正传》《祝福》《在酒楼上》
- **朱自清**(1898–1948,卒逾 50 年,大中华区公有领域;所收各篇发表于
  1923–1927,美国亦公有领域):《背影》《荷塘月色》《温州的踪迹》《航船中的文明》

跨内容基准的 16 篇朱自清语料同样来自中文维基文库，且只选自 1924 年
《踪迹》和 1928 年《背影》所收篇目。每一页的 revision id 固定在
`fetch_zhuziqing_benchmark_corpus.py`，脚本只抽取正文 `<p>`、清理零宽字符
并排除站点版权模板；2026-08-19 生成版本正文 43,384 字，SHA-256 为
`28a4b86276cbd5d76c0dfd3c9fb13deaf9955e491cc31304b5c67c1be3e26638`。
需要核验或有意更新时执行：

```powershell
cd backend
python tests/golden/style_reference/fetch_zhuziqing_benchmark_corpus.py
python tests/golden/style_reference/fetch_zhuziqing_benchmark_corpus.py --write
```

**硬约束(《手册》§9.2 / 附录 C B9)**:本目录严禁出现当代受版权保护 IP
的文本或关键词;`test_style_reference_golden.py` 内置关键词守卫用例,
命中即 fail。

## 期望文件再生成

corpus、分段规则或启发式分类**有意**变更后,重新生成 expected(2026-09-24 起 expected 不再含 v2 指标块):

```powershell
cd backend
python tests/golden/style_reference/regen_expected.py
```

(脚本在隔离临时库上跑真实 ingest 管线——启发式分类,无 LLM,结果确定。)

## 本地私有语料验证(不入仓库)

想用自己的书(如受版权保护的当代小说)验证摄取/统计/抄袭检测,
设环境变量指向本地 TXT(UTF-8 或 GB18030)后单独跑:

```powershell
$env:NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS = "C:\path\to\本地参考书.txt"
python -m pytest tests/test_style_reference_local_corpus.py -v
```

未设该变量时这些用例自动跳过;本地书内容永不进入仓库。
