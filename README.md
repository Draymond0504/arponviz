# ApronOps

机场机坪自主作业协同与异常处置平台（仿真 / 数字孪生 MVP）。

完整功能介绍与操作验收见 [一次机坪保障班次：完整测试案例](docs/acceptance-walkthrough.md)，包含可运行场景、三策略实测结果、五个页面操作步骤与故障/重启边界测试。

**边界声明**：本项目是面向封闭机坪场景的仿真与运营协同平台，**不**声称可直接部署到真实机场机坪。真实机坪涉及适航、机场运行规程、通信与安全认证，个人项目不越过这些边界；所有阈值、时长、坐标均为模拟示意值。

## 快速开始

### 本地运行

```bash
pip install -r requirements.txt
pytest                                   # 单元测试
uvicorn apronops.api.main:app --reload   # 启动 API（含后台模拟循环），默认 :8000
streamlit run apronops/dashboard/app.py  # 另开终端启动看板，默认 :8501
```

### Docker Compose

```bash
docker compose up --build
```

- API: http://localhost:8000/docs
- 看板: http://localhost:8501

## 目录结构

见项目根目录下的模块划分：`apronops/models.py`（数据模型）、`state_machine.py`（状态机）、`scheduler.py`（调度）、`anomaly.py`（异常检测）、`simulator.py`（模拟循环）、`storage.py`（SQLite 持久化）、`metrics.py`（KPI）、`api/`（FastAPI）、`dashboard/`（Streamlit）。

## 数据说明

当前所有数据均为**模拟数据**，位于 `data/*.yaml`：

| 文件 | 内容 | 对应真实数据来源（生产环境，本项目不接入） |
|---|---|---|
| `apron_layout.yaml` | 机坪作业点坐标与连接边 | 机场 AIP/平面图、GIS 数据 |
| `vehicles_seed.yaml` | 车辆类型、初始位置、速度、电量 | 车辆厂商规格书 |
| `tasks_seed.yaml` | 模拟任务（类型、起终点、优先级、截止时间） | 航班时刻表 / FIDS / 地面服务工单系统 |

保障作业标准时长（`simulator.py` 中 `SERVICE_DURATION_S`）、异常检测阈值（`anomaly.py`）、安全余量等同样为示意常数，如需替换为真实运营数据，需机场运行方审批并对接相应系统，超出本项目范围。

## 外部依赖

无需安装任何外部仿真软件（不需要 ROS2/Gazebo/SUMO/AnyLogic）。全部逻辑为纯 Python 自实现的离散时间步模拟循环，`pip install -r requirements.txt` 即可运行整个项目。

后续如强化"具身智能"叙事，可选接入 ROS2 + Nav2 + Gazebo（需 Ubuntu/WSL2 环境）；如需更真实的路网/交通仿真，可选接入 SUMO（`pip install eclipse-sumo`）。两者均不在当前骨架范围内。


## 独立维护

GitHub 仓库：https://github.com/Draymond0504/arponviz

```bash
git clone git@github.com:Draymond0504/arponviz.git
cd arponviz
```

本仓库从 `Draymond0504/airport-marl` 拆出，保留初始导入提交供追溯。
当前工作树仅包含 ApronOps 平台，平台使用自身车辆仿真与规则调度。
算法和论文研究代码继续维护于 https://github.com/Draymond0504/airport-marl 。

使用 Python 3.11 或 3.12，先创建本地环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests -q
```

前端入口为 `apronops/dashboard/app.py`，后端入口为
`apronops/api/main.py`。默认前端连接 `http://localhost:8000`；
通过 `APRONOPS_API_BASE` 可指定后端地址。
`APRONOPS_DB_PATH` 指定 SQLite 数据库位置（默认 `apronops-operations-v1.db`），数据文件不加入 Git。

修改前端后需同时启动 API 和 Streamlit 核对页面；涉及业务逻辑时运行
`pytest tests -q`。两套工程尚未通过 API 接入，不依赖 MAPPO 模型权重。


## 独立车辆道路（基于 Layout H）

默认底图为根据最新 Layout H 机位布局推演的车辆专用路网：外围环路、分区服务路和机位作业支路，共 98 个车辆节点、98 条双向路段、30 个机位服务点和 5 个车辆基地。车辆不再沿飞机滑行道寻路；原始 103 节点飞机图保存在 `data/aircraft_layout_h.yaml`，看板可选择叠加参照。

车辆道路按 20 / 12 / 5 km/h 分级限速。11 个交叉点已加入仿真空地互锁：飞机获准占用期间车辆在区外等待，区内车辆先清空。道路尺寸及冲突区范围均为推演假设，未接入真实飞机定位或放行系统。看板支持选择起终点预览车辆路线。

配置依据、典型路线与维护步骤见 [车辆道路推演说明](docs/vehicle-roads.md)。生成脚本为 `scripts/build_vehicle_layout.py`，默认车辆图为 `data/apron_layout.yaml`。车辆继续使用平台规则仿真，尚未接入 MAPPO。


## 运行与异常处置（第一阶段）

车辆未取货时发生通信故障，任务可重新分配；已经取货或服务中的任务保留，故障全部解除后从原位置和阶段续跑。道路封闭实际影响寻路，无路可走时等待，解除道路告警后恢复通行。

看板地图与状态每 2 秒刷新，支持封路、恢复和事件查看。创建任务、处置告警与每次推进均保存运行快照，API 重启恢复最后成功提交的现场。使用单个 uvicorn worker；已有旧版数据库或不同路网需要使用独立数据库，避免混入旧状态。

操作方式、接口、恢复边界和指标定义见 [运行与异常处置](docs/operations.md)。后续航班任务、飞机穿越约束、批量实验与 MARL 接入按 [分阶段开发路线](docs/development-roadmap.md) 推进。


## 航班保障与飞机穿越（第二阶段）

“航班与穿越”页面可创建航班并生成行李、配餐、保障后检查任务，支持延误更新时间窗、前置任务门控及合成穿越场景。交叉点区分预测窗口、申请窗口与实际授予窗口，车辆与飞机在仿真冲突区内互斥通行。

已有版本 1/2/3 运行快照可自动读取，升级后保存为版本 4，保持原有任务现场。使用方法、模型假设及接口见 [航班与穿越说明](docs/flights-and-crossings.md)。这仍是规则仿真，独立实验接口与基线见下节。


## 可复现实验与离线回放（第三阶段）

无需启动 API 或数据库即可运行规则策略，在同一种子生成的航班、穿越和故障输入上对比。实验会保存配置、源码版本、事件、轨迹、任务表及指标，并保留未完成任务和中断结果。

```bash
pip install -r requirements-experiments.txt
python -m apronops.experiments.run \
  --scenario configs/experiments/routing.yaml \
  --seeds 1 2 3 \
  --output results/experiments/routing-batch-001
```

每次使用新的输出目录。看板“实验回放”可选择本机实验或上传云服务器生成的 `replay.json`，完整播放动画、暂停、倍速和拖动进度，查看车辆、任务、封路与飞机管制；支持下载单个 HTML 离线播放，不依赖在线现场。批量运行、指标定义、可重复性边界和已知基地阻挡问题见 [实验与回放](docs/experiments.md)。基地泊位、排队与滚动优化见下一节。


## 基地容量与滚动优化（第四阶段）

五个基地支持配置泊位和办理时长、FIFO 进站排队及泊位预留。停驻车辆停在逻辑泊位内，避免挡住道路；持续满位时可触发挪车或节点改道，基地资源循环等待会单独记录。恢复动作保留物理位置、任务与故障状态。

新增 `rolling_optimization`，每 15 秒联合比较任务与车辆，考虑公开穿越预测、基地排队和服务时间。运行总览可切换三种规则，实时页与实验回放均显示泊位、队列及恢复进度。规则、边界及接口见 [基地与滚动优化](docs/depots-and-optimization.md)。

本阶段仅维护平台的规则仿真与优化，不涉及 MARL。
