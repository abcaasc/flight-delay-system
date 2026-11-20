# 基于 Spark 的航班延误预测与航线网络态势可视化系统 · 后端

> 毕业设计题目：《基于 Spark 的航班延误预测与航线网络态势可视化系统》
> 学生：陈向东（大数据 2302，40516230232）　指导教师：姚杰（副教授）
> 本仓库为**后端服务**（数据接入 → 数仓加工 → 指标服务 → REST 接口），前端 Vue3 + ECharts 另仓承接。

2026-11-17 已完成后端正确性与稳定性修复，116 项测试通过，并在隔离数仓验证了 9,000 条真实 BTS 样本及三个 NOAA 站点的完整预测链路。修改、复现命令和旧数据升级说明见 [后端优化与验证说明](docs/后端优化与验证说明.md)。

---

## 一、这套后端解决什么问题

把「120 个月 / 约 6300 万条逐航班流水 + 1142 个气象站文件 + 机场航线维表」变成「可预测、可解释、可下钻的态势接口」：

| 开题报告功能模块 | 后端落点 |
| --- | --- |
| （1）数据采集模块 | `app/services/datasource.py`、`app/etl/ods.py`：9 个数据集登记、通道 A/B/C、MD5 与行数校验、断点续跑 |
| （2）数据清洗与存储模块 | `app/etl/dwd.py` + `app/services/engine.py`：R-01~R-07 清洗、航班链重建、气象窗口对齐、按年月写 Parquet |
| （3）航线网络多维统计 | `app/etl/dws.py`：机场×小时 / 航线×天 / 机场×30 分钟 三层预聚合 |
| （4）可视化看板 | `app/api/dashboard.py`：总览、网络图、热力图、排行、传播路径（桑基） |
| （5）交互查询 | `app/api/flights.py`：多条件筛选、分页、CSV 导出（≤1000 行） |
| （6）延误预测 | `app/services/model.py`：LightGBM 二分类 + SHAP；三级分类/回归目前为基线评估 |
| （7）系统管理 | `app/api/auth.py`、`app/api/system.py`：JWT + 四角色 RBAC、审计、告警、用户管理 |
| （8）模型评估与跨域验证 | `app/api/model_api.py`（P5/P6）、`stats.crossdomain_*`（美国 vs 中国 STPN） |

---

## 二、技术栈（与开题报告一致）

| 层 | 选型 |
| --- | --- |
| Web | Python 3.10 + **Flask 3.1**（Blueprint 分模块 + 应用工厂） |
| 大数据 | **PySpark 3.4**（`app/services/spark_session.py`，参数按设计说明书 §4.1 调优）；当前 ETL 核心仍以 pandas 处理，不能视为全分布式计算 |
| 数仓 | **Parquet + Snappy**，Hive 风格分区 `year_month=` / `year=`，四层十七表 |
| 业务库 | **MySQL 8.0**（SQLAlchemy 2.0 + PyMySQL）；未配置时自动落 SQLite（开发零依赖） |
| 缓存 | **Redis 7**（看板 600s / 查询 300s）；不可用时降级进程内 TTL 缓存 |
| 建模 | **LightGBM 4.5.0**（`pred_contrib=True` 直接拿精确 SHAP，无需额外装 shap）+ 内置可解释线性基线 |
| 认证 | JWT(HS256) + PBKDF2 口令哈希 + 角色装饰器 |

---

## 三、目录结构

```
backend/
├── run.py                      # 入口（python run.py 或 waitress-serve run:app）
├── requirements.txt
├── .env.example                # 全部可配置项（含注释）
├── app/
│   ├── __init__.py             # 应用工厂：日志/TraceID/统一错误/蓝图/CLI
│   ├── config.py               # 唯一配置源：路径、口径阈值、黑名单、Spark 参数
│   ├── extensions.py           # SQLAlchemy 引擎与 session（纯 2.0，不用 Flask-SQLAlchemy）
│   ├── models/                 # MySQL 业务表：用户/审计/作业/审计明细/清单/死信/模型版本/告警/案例
│   ├── api/                    # 11 个蓝图：auth/dashboard/flights/predict/model/data/system/ai...
│   ├── services/
│   │   ├── catalog.py          # ★ 四层十七表的唯一契约（字段/分区/主键/白名单）
│   │   ├── engine.py           # ★ 双引擎（pandas / Spark）+ 分区裁剪 + 原子覆盖写
│   │   ├── datasource.py       # 9 个数据集登记、扫描、MD5 校验、周期解析
│   │   ├── metrics.py          # ★ 指标口径唯一真源（准点率/延误率/取消率）
│   │   ├── stats.py            # 看板与查询服务（总览/网络/排行/传播/跨域/质量）
│   │   ├── model.py            # 预测、训练、校准、SHAP 归因
│   │   ├── predict_service.py  # 单航班在线特征装配（无泄漏）
│   │   ├── ingest.py           # 入湖/ETL 作业编排（线程 + 数据集级锁 + 进度落库）
│   │   ├── weather_map.py      # 机场→ICAO→ISD 站号映射
│   │   ├── cache.py / spark_session.py
│   ├── etl/                    # ODS / DIM / DWD / DWS / ADS 作业 + ISD 编码解析
│   └── utils/                  # 统一响应、参数校验、JWT/RBAC、时间工具、日志
├── scripts/
│   ├── inspect_warehouse.py    # 数仓巡检（就绪/行数/分区/文件数）
│   ├── build_one.py            # 单表重建（调试用）
│   ├── smoke_test.py           # 60 项接口冒烟（含权限与验收项）
│   └── gen_docs.py             # 自动生成 数据字典.md / 接口清单.md
├── tests/                      # 116 个 pytest 用例（清洗规则/编码解析/指标/引擎/接口/权限）
├── sql/                        # MySQL 建库脚本（可选，ORM 也能建表）
└── docs/                       # 数据字典、接口清单、系统测试用例、部署与运维
```

---

## 四、快速开始

```powershell
cd backend

# 1) 安装依赖
python -m pip install -r requirements-model.txt

# 2) 配置（可选：不配也能跑，默认 SQLite + 进程内缓存 + 数据源默认路径）
copy .env.example .env

# 3) 初始化：建表 + 预置 4 个账号 + 默认告警规则
python -m flask --app run.py init-db
python -m flask --app run.py seed-users

# 4) 环境自检（依赖 / 数据源 / 数仓 / 引擎）
python -m flask --app run.py doctor

# 5) 一键跑通全链路（示例：2015-01 一个月，约 2 分钟）
python -m flask --app run.py pipeline --periods 2015-01

# 6) 启动服务
python run.py                       # http://127.0.0.1:5000
```

预置账号（口令即用户名 + `123`）：

| 账号 | 口令 | 角色 | 菜单 |
| --- | --- | --- | --- |
| guest | guest123 | 访客/评审 | 仅态势看板 |
| ops | ops123 | 运行监控员 | 看板 + 预测分析 |
| analyst | analyst123 | 数据分析员 | + 模型中心（可训练） |
| admin | admin123 | 管理员 | + 数据管理 + 系统管理 |

```powershell
# 登录拿 token
curl -X POST http://127.0.0.1:5000/api/v1/auth/login ^
     -H "Content-Type: application/json" ^
     -d "{\"username\":\"admin\",\"password\":\"admin123\"}"
```

---

## 五、常用命令

| 命令 | 作用 |
| --- | --- |
| `flask --app run.py data-scan` | 扫描 9 个数据集，打印文件数/体量/覆盖周期/缺失周期 |
| `flask --app run.py verify --dataset bts --periods 2015-01` | 文件级校验（MD5 + 字节数；可加 `--no-md5`） |
| `flask --app run.py dims` | 重建维表（机场 9054 / 航司 / 日历 3653 / 节假日） |
| `flask --app run.py ingest --dataset bts --periods 2015-01,2015-02 --mode full` | 入湖（ODS） |
| `flask --app run.py etl --periods 2015-01` | 数仓加工 DWD → DWS → ADS |
| `flask --app run.py train --periods 2015-01 --task cls15 --online` | 训练 + 评测 + 批量预测 + 上线 |
| `flask --app run.py pipeline --periods 2015-01 --limit 30000` | 一键全链路（`--limit` 便于快速验证） |
| `flask --app run.py doctor` | 环境自检 |
| `python scripts/inspect_warehouse.py` | 数仓巡检 |
| `python scripts/smoke_test.py --base http://127.0.0.1:5000` | 60 项接口冒烟 |
| `python -m pytest tests -q` | 单元/接口测试（116 项，含建模依赖） |
| `python scripts/gen_docs.py` | 重新生成数据字典与接口清单 |

**120 个月全量**：`pipeline` 默认只跑最新一个月，全量请按月循环（单月约 60–120 s，取决于磁盘）：

```powershell
python -m flask --app run.py etl --periods 2015-01,2015-02,...,2024-12
```

---

## 六、数据接入：9 个数据集 / 3 条通道 / 3 道闸门

| 数据集 | 通道 | 源文件 | 目标表 | 分区 |
| --- | --- | --- | --- | --- |
| BTS 航班准点 | A（命令行批处理） | 120 个 `*_YYYY_M.zip`（3.04 GB） | `ods_bts_flight` | `year_month` |
| NOAA ISD 逐时气象 | A | 1142 个站点 CSV（6.5 GB） | `ods_isd_weather` | `year` |
| US Weather Events | A | 1 个 zip（92.6 MB） | `ods_weather_event` | `year` |
| OurAirports | C（页面直传/本地） | `airports.csv` 等 3 个 | `dim_airport` | 不分区 |
| OpenFlights | C | 4 个 `.dat` | `dim_airline` | 不分区 |
| IEM ASOS 样本 | C | 5 个 CSV | 交叉验证解析器 | — |
| STPN 中美张量 | A | 8 个 `.npy`（158 MB） | 不入湖，跨域接口直读 | — |
| 清单与字典 | B（控制面） | `manifest.csv` | `ods_file_manifest` | 不分区 |

**通道语义**：A = 数据面本地读取（字节不经浏览器）；B = 只传指令与清单；C = 页面直传（单文件 ≤50 MB，后缀白名单 + 防路径穿越）。

**三道闸门**（`/api/v1/ingest/verify` 与 ETL 内置）：

1. **文件级**：MD5、字节数、周期完整（BTS 期望 120 个月，缺一期即 `passed=false`）、月环比体积波动 ≤30%；
2. **字段级**：`origin`/`dest` 必须三位大写字母、`flight_date ∈ [2015-01-01, 2024-12-31]`、`dep_delay ∈ [-300, 1500]`；
3. **行级**：唯一键 `(FlightDate, Reporting_Airline, Flight_Number, Origin)` 去重、时间逻辑校验，剔除与标记全部计数留痕。

> 为什么解压放在入湖作业里：Spark 读不了 `.zip` 容器，且「解压」必须可重跑、可审计。本实现按「单月解压 → 转 Parquet → 删中间 CSV」执行，单月磁盘峰值约 300 MB（不是 13 GB）。

---

## 七、数仓：四层十七表

详见 [`docs/数据字典.md`](docs/数据字典.md)。要点：

| 层 | 表 | 说明 |
| --- | --- | --- |
| ODS | `ods_bts_flight` / `ods_isd_weather` / `ods_weather_event` | 保真落地（BTS 关键字段全字符串；ISD 保留 NOAA 原始编码串） |
| DIM | `dim_airport` / `dim_airline` / `dim_date` / `dim_holiday` | 广播小表；机场含跑道数与 ICAO（气象站映射用） |
| DWD | `dwd_flight_detail` / `dwd_weather_hourly` | 清洗 + 航班链 + 气象时间窗对齐 |
| DWS | `dws_airport_hourly` / `dws_route_daily` / `dws_delay_network` / `dws_flight_features` | 看板与建模的预聚合 |
| ADS | `ads_delay_pred` / `ads_model_metric` / `ads_airport_rank` / `ads_anomaly_alert` | 预测结果、评估指标、排行、预警 |

**双引擎**：`QUERY_ENGINE=auto` 时有 PySpark 就用 Spark 读同一套 Parquet，否则用 pyarrow（带分区裁剪）。ETL 写入永远是 Parquet，两种引擎可互读 —— 论文里做「加速比」实验只需切 `QUERY_ENGINE=spark`。

---

## 八、指标体系（唯一口径）

```
scheduled  计划航班量 = 全部记录
cancelled  取消量
operated   实际执行 = scheduled − cancelled        ← 准点率的分母不含取消
delayed    延误量 = operated 中 DepDelay ≥ 15 分钟
ontime_rate = (operated − delayed) / operated
delay_rate  = delayed / operated
cancel_rate = cancelled / scheduled
avg_delay   = operated 的 DepDelay 均值（保留负值，提前起飞计负）
```

阈值全部外置在 `.env`（`ONTIME_THRESHOLD_MIN` / `SEVERE_DELAY_MIN` / 延误值域），代码里没有魔法数字。

---

## 九、预测与归因

- **三任务**：`cls15`（二分类，延误 ≥15 min）、`level3`（三级分类）、`reg`（延误分钟回归）。
- **时间外推切分**：按完整日期分组，近似 70/15/15，训练、验证、测试日期互不重叠；至少需要三个不同日期。
- **截距校准**：基线截距仅用训练集估计，分类阈值仅用验证集选择；保存在各自版本产物中，训练不修改线上参数。
- **归因**：LightGBM 输出 logit 空间 SHAP；`base_logit + 各项贡献` 经 sigmoid 重建概率。`impact_pp` 为局部近似，不能按概率百分点精确相加。
- **泄漏隔离**：在线历史统计只读取预测日期之前七个自然日；气象按机场时区转换为 UTC 后取窗口；过站基准仅从历史样本估计。
- **输出含义**：在线 `pred_minutes` 是概率映射参考值，`pred_level` 是风险分档，不能视为独立回归或三级分类模型结果。
- **当前验证**：运行记录见 `instance/verification/last_report.json`。下文旧版三万行样本的结果不代表修复后指标，不能直接引用为当前论文实验结果。

---

## 十、主要接口

完整清单见 [`docs/接口清单.md`](docs/接口清单.md)（由 `url_map` 自动生成，共 64 个路由）。按页面分组：

| 页面 | 接口 |
| --- | --- |
| P1 准点率总览 | `/overview`、`/overview/kpi`、`/overview/trend`、`/overview/distribution`、`/overview/reasons` |
| P2 航线网络 | `/network`（节点+连边，含经纬度）、`/network/heatmap` |
| P3 机场排行/详情 | `/airport/rank`、`/airport/{iata}`、`/airport/{iata}/rank` |
| P4 延误传播 | `/propagation`（桑基：前序延误档 → 本班延误档） |
| 查询导出 | `/flights`、`/flights/{flight_sk}`、`/flights/export` |
| 预测与归因 | `/predict`、`/predict/high-risk`、`/predict/airport`、`/explain/{flight_sk}`、`/explain/global` |
| 模型中心 | `/model/versions`、`/model/compare`、`/model/generalization`、`/model/train`、`/model/online`、`/model/feature-spec` |
| 数据管理 | `/ingest/datasets`、`/ingest/verify`、`/ingest/jobs`、`/ingest/failed`、`/upload/staging`、`/upload/promote`、`/quality/report`、`/warehouse/tables` |
| 系统管理 | `/system/health`、`/system/freshness`、`/system/job/stats`、`/system/audit`、`/system/users`、`/system/alerts/*` |
| 跨域与 AI | `/crossdomain/china`、`/crossdomain/compare`、`/ai/chat` |

统一响应：`{"code": 0, "message": "success", "data": {...}, "trace_id": "..."}`；错误码分 40x 参数 / 41x 权限 / 50x 服务 / 52x 数据缺失。

---

## 十一、配置项（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_ROOT` | `C:\Users\Administrator\Desktop\航班延误预测数据源` | 9 个数据集根目录 |
| `WAREHOUSE_ROOT` | `./data/warehouse` | Parquet 数仓目录 |
| `QUERY_ENGINE` | `auto` | `auto` / `pandas` / `spark` |
| `SPARK_MASTER` | `local[6]` | 留 2 核给 OS 与前端 |
| `DATABASE_URL` | 空 → SQLite | 生产填 `mysql+pymysql://user:pwd@host:3306/flight_delay?charset=utf8mb4` |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 连不上自动降级进程内缓存 |
| `ONTIME_THRESHOLD_MIN` | `15` | 准点判定阈值（分钟） |
| `SEVERE_DELAY_MIN` | `60` | 严重延误阈值（分钟） |
| `STAGING_MAX_BYTES` | `52428800` | 页面直传单文件上限（50 MB） |
| `EXPORT_MAX_ROWS` | `1000` | 导出上限（防拖库） |
| `DEEPSEEK_API_KEY` | 空 | 为空时 AI 助手走本地统计摘要并标 `degraded=true` |

---

## 十二、与论文/答辩的对应

| 论文章节 | 用这里的什么 |
| --- | --- |
| 数据说明与合规 | `data-scan` 输出（9 数据集 / 3.04 GB BTS / 1142 站点）、`verify` 报告、`/ingest/datasets` |
| 数据预处理 | `docs/数据字典.md`、`/quality/report`（清洗前后行数与剔除原因分布）、`tests/test_timeutil.py`（R-02 正反例） |
| 数仓设计 | 四层十七表 + 分区策略 + 双引擎；`/warehouse/tables` 实拍 |
| 模型实验 | `/model/compare`（多版本指标）、`/model/generalization`（跨机场矩阵）、`tests` 里的可复现种子 |
| 可视化 | 各接口返回结构即为前端图表数据契约（节点/连边/矩阵/桑基） |
| 系统测试 | [`docs/系统测试用例.md`](docs/系统测试用例.md)：60 项冒烟 + 79 项单测，含验收项 U-01~U-15、V-8 |

**可复现四件套**：固定随机种子（`RANDOM_SEED=42`）、模型版本号（`lightgbm_cls15_YYYYMMDD_HHMMSS`）、文件 MD5 清单（`ods_file_manifest`）、作业参数入档（`sys_job_run.stages`）。

---

## 十三、已知限制

1. **PySpark 与 JDK 版本**：PySpark 3.4 只支持 JDK 8/11/17；若本机 `JAVA_HOME` 指向 JDK 21，请改成 JDK 17 再开 `QUERY_ENGINE=spark`（`/system/health` 会如实报告原因并自动降级 pandas）。
2. **气象站点缺失**：并非每个机场每年都有 ISD 站点文件（如 2015 年 115 个文件中命中 78 个）。这类机场在 DWD 里标 `weather_available=0`，不做全局均值填充（会抹平气候差异）。
3. **中国域只有聚合粒度**：STPN 为机场×30 分钟，仅支持机场/时段级指标与跨域对照，**不支持中国单航班预测**（与任务书一致）。
4. **单机并发控制**：作业锁是进程内锁，多进程部署（gunicorn 多 worker）需换成 Redis 锁；本项目按单机答辩场景实现。
5. **AI 助手**：默认无 Key 走本地模板摘要；配置 `DEEPSEEK_API_KEY` 后自动切真实模型，失败仍降级不白屏。

---

## 十四、测试与验收

```powershell
python -m pytest tests -q                 # 独立 SQLite 和临时数仓，无需构建前端
python scripts/smoke_test.py --base http://127.0.0.1:5000   # 60 passed
```

配置了 LightGBM 和完整数仓时，可复现表单版候选模型的留出测试。

```powershell
python -m flask --app run.py train --profile form --periods 2024-10,2024-11,2024-12 --sample-per-month 50000 --no-predict
```

表单模型使用日期、计划时刻和机场之间的大圆距离，并在观测可用时加入计划起飞前的气象数据。缺测的气象特征保留为空。训练、阈值校准和留出测试按互不重叠的日期切分。数据管理的「模型验证」会读取 `instance/form-model-validation.json`，也可通过 `FORM_MODEL_REPORT` 指定路径。命令会登记候选版本，有效的线上模型由管理员单独选择。

表单候选默认使用少误报阈值。在验证集上保持召回率至少 40%，再选择准确率最高的阈值。报告在同一留出集上对比少误报阈值、F1 优先阈值和全部判为准点的基线，并给出误报与漏报架次。

总览、趋势、网络和逐班查询会返回月份覆盖情况。演示明细的抽样月份由数仓目录下的 `_sampled_flight_months.json` 标明。新提交的后台作业每 30 秒更新心跳。心跳超时后，作业标为「已中断」，管理员可用保存的提交参数重新提交。

覆盖：清洗规则正反例、ISD 编码解析（含向量化与标量一致性）、指标口径、数仓读写幂等、认证与四角色权限、参数校验、上传白名单、周期拦截、统一响应结构、数据字典生成。

### 本机实测（2015-01 单月 + ISD 2015 全年 + WeatherEvents 20 万行抽样）

```
表                        层    就绪           行数   分区   文件数      体积MB
ods_bts_flight           ods  ✅         30000    1     8      0.93
ods_isd_weather          ods  ✅       1355124    1    40     64.82
ods_weather_event        ods  ✅        200000    7    56      4.57
dim_airport              dim  ✅          9054    1     1      0.67
dim_airline              dim  ✅           654    1     1      0.03
dim_date                 dim  ✅          3653    1     1      0.03
dim_holiday              dim  ✅           110    1     1      0.00
dwd_flight_detail        dwd  ✅         30000    1     8      2.43
dwd_weather_hourly       dwd  ✅        633302    1     8      6.75
dws_airport_hourly       dws  ✅         13964    1     8      0.24
dws_route_daily          dws  ✅          9604    1     8      0.15
dws_delay_network        dws  ✅         17404    1     8      0.20
dws_flight_features      dws  ✅         30000    1     8      1.43
ads_delay_pred           ads  ✅         30000    1     4      1.06
ads_model_metric         ads  ✅            20    1     7      0.06
ads_airport_rank         ads  ✅          7440    1     2      0.05
ads_anomaly_alert        ads  ✅           228    1     1      0.01
--------------------------------------------------------------------
合计                                    2370557                83.41 MB
```

关键指标：准点率 79.83%、平均起飞延误 10.49 分钟、取消率 2.23%；LightGBM 测试集 F1 0.39 / AUC 0.75；
气象站点覆盖 78/115（其余机场 `weather_available=0`）；中美同粒度准点率 80.3% vs 79.6%。

> ISD 数据有两条可用路径：`flask ingest --dataset isd --periods 2015` 先入 ODS（1.35M 行 / 64.8 MB）
> 再由 DWD 从 ODS 解析；也可让 DWD 直接读站点文件。DWD 会检查 ODS 的站点覆盖率，
> **低于 90% 自动回落到站点文件**并写明原因，避免"只入湖了部分站点"造成静默缺气象。


---

## 十五、许可与数据来源

| 数据源 | 许可 | 引用要求 |
| --- | --- | --- |
| BTS On-Time Performance | 美国政府公开数据 | 注明来源与下载日期 |
| NOAA NCEI ISD | 公开数据 | 注明站点与时间范围 |
| OurAirports | Public Domain | 建议注明来源 |
| OpenFlights | ODbL | **需署名** |
| STPN（IEEE DataPort） | 数据集页面许可 | 引用 DOI `10.21227/4e59-r355` 及原论文 |
| US Weather Events | Kaggle 公开数据集 | 注明来源 |

论文"数据来源"一节需给出：数据名称、发布机构、获取 URL、下载日期、时间范围、记录条数 —— 这几项在 `manifest.csv` 与 `/api/v1/ingest/datasets` 里都有原始记录。
