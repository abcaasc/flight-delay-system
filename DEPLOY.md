# 部署到 GitHub 并发布公网演示

本文把「本地项目」变成「别人点开链接就能看的在线系统」，分三步：**推 GitHub → 选一个托管平台 → 填上演示地址**。

> 关键认知：GitHub 本身只托管代码，**不能直接运行 Flask 后端**（GitHub Pages 只能放静态文件）。
> 本项目的做法是把前端构建产物塞进后端镜像，做成**一个容器、一个端口**，再把容器放到支持 Docker 的免费平台上。

---

## 0. 前置条件

| 需要 | 说明 |
| --- | --- |
| GitHub 账号 | https://github.com/signup |
| Git | 本机已装（`git --version` 有输出） |
| 一个托管平台账号 | 二选一：Render（推荐，与 GitHub 打通）/ Hugging Face Spaces |

---

## 1. 推送到 GitHub（✅ 已完成）

**仓库已建好并推送**：<https://github.com/abcaasc/flight-delay-system>（`main` 分支，提交 `db46acb`，仓库体积约 26 MB）。

下面的 1.1~1.3 是原始步骤，换机器 / 重建仓库 / 换账号时照做即可。

### 1.1 网页上新建仓库

打开 https://github.com/new ：

- **Repository name**：`flight-delay-system`
- **Visibility**：`Public`（公开才方便导师/评审直接访问；不想公开可选 Private，但 Render 免费档需要授权访问私有库）
- **不要**勾选 "Add a README file" / ".gitignore" / "license"（仓库里已经有了，勾了会冲突）

创建后记下仓库地址，例如 `https://github.com/<你的用户名>/flight-delay-system.git`。

### 1.2 本地提交并推送

在项目根目录（`flight-delay-system`）执行：

```powershell
git init
git add .
git commit -m "feat: 航班延误预测与航线网络态势可视化系统（含公网演示版）"
git branch -M main
git remote add origin https://github.com/<你的用户名>/flight-delay-system.git
git push -u origin main
```

首次推送会让你输入用户名和密码 —— **GitHub 已不接受账号密码**，密码处要填 **Personal Access Token（PAT）**：

1. 打开 https://github.com/settings/tokens → **Generate new token (classic)**
2. Note 随便写，Expiration 选 30 天，勾选 **`repo`**（私有库还需要 `workflow`）
3. 生成后复制 `ghp_...`，粘贴到命令行提示的 "Password" 位置

> 也可以用 SSH：`git remote set-url origin git@github.com:<用户名>/flight-delay-system.git`，前提是本机已配置 SSH key。
> Windows 上更省事的办法是装 **GitHub Desktop**，登录后点 Publish repository。

### 1.3 检查有没有把不该传的东西传上去

```powershell
git count-objects -vH          # 仓库体积，应该在 40 MB 左右
git ls-files | Select-String "\.env$|warehouse/|node_modules|\.venv"   # 应该没有输出
```

仓库里**必须包含**：`backend/data/demo/warehouse/`（约 30 MB 演示数仓）、`backend/data/models/`（2.5 MB 模型）。
**绝不能包含**：`backend/.env`（含数据库密码）、`backend/data/warehouse/`（12 GB 完整数仓）、`node_modules/`、`.venv/`。

---

## 2. 选一个托管平台

### 方案 A：Render（推荐，免费、与 GitHub 自动联动）

免费档：512 MB 内存 / 0.1 CPU / **15 分钟无访问自动休眠**，下次访问约 30–60 秒冷启动。演示够用。

仓库根目录已经有 `render.yaml`，所以**不用手填任何部署参数**：

1. 打开 https://render.com → **Sign in with GitHub**（授权时选 `Only select repositories`，只勾这个仓库）
2. 右上角 **New +** → **Blueprint** → 选 `abcaasc/flight-delay-system` → **Connect**
3. 识别到 `render.yaml` 后点 **Apply**，等 5–8 分钟（云端要装 Node 构建前端 + pip 装依赖）
4. 顶部出现的地址就是公网演示地址：`https://flight-delay-system-xxxx.onrender.com`

**以后更新代码只需 `git push`**，Render 会自动重新构建部署。

<details>
<summary>不想用 Blueprint 的话，也可以手动建 Web Service</summary>

| 字段 | 值 |
| --- | --- |
| Language / Runtime | **Docker** |
| Branch | `main` |
| Instance Type | **Free** |
| Health Check Path | `/api/v1/system/health` |

</details>

> 免费档的坑：示例数据由 `backend/data/demo/warehouse` 提供，镜像内已经带好，不依赖任何外部存储。
> 若 512 MB 内存不够（日志出现 `Killed`），把 `Dockerfile` 里的 `--threads=8` 调小，或改用下面的方案 B。

### 方案 B：Hugging Face Spaces（免费 16 GB 内存，休眠更慢）

免费档：16 GB 内存 / 2 vCPU / **48 小时无访问才休眠**，唤醒约 30 秒。内存比 Render 宽裕得多。

1. 注册 https://huggingface.co/join
2. **New → Space**：Name 填 `flight-delay-system`，**SDK 选 Docker**，Hardware 选 `CPU basic (free)`，Visibility 选 `Public`
3. Space 仓库的 `README.md` 必须是「带 YAML 头的版本」，否则 Space 起不来。把下面内容整体替换 Space 仓库根目录的 `README.md`：

   ```markdown
   ---
   title: 航班延误预测与航线网络态势可视化系统
   emoji: ✈️
   colorFrom: blue
   colorTo: indigo
   sdk: docker
   app_port: 7860
   pinned: false
   ---

   基于 Spark 的航班延误预测与航线网络态势可视化系统（毕业设计演示）。
   后端 Flask + Parquet 数仓 + LightGBM，前端 Vue3 + ECharts。
   ```

4. 把代码推上去（HF 的 git 远端和 GitHub 是两套）：

   ```powershell
   git remote add hf https://huggingface.co/spaces/<你的用户名>/flight-delay-system
   git push hf main:main
   ```

   用户名密码处填 HF 的 **Access Token**（https://huggingface.co/settings/tokens ，权限选 `write`）。

5. 构建完成后地址是：`https://huggingface.co/spaces/<你的用户名>/flight-delay-system`
   （应用本体在 `https://<用户名>-flight-delay-system.hf.space`）

> 注意：HF Space 走的是 `PORT=7860`，Dockerfile 已经默认 7860，和 `app_port` 对齐，不用改。

### 方案 C：自有服务器 / 实验室机器

```bash
# 在服务器上
git clone https://github.com/<你的用户名>/flight-delay-system.git && cd flight-delay-system
docker build -t flight-delay-system .
docker run -d --name flight-delay --restart=unless-stopped -p 80:7860 flight-delay-system
```

用 Nginx 挂域名时：

```nginx
server {
    listen 80;
    server_name demo.example.com;
    client_max_body_size 60m;
    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

---

## 3. 部署后自检

打开 `<你的公网地址>`，依次确认：

| 检查项 | 期望 |
| --- | --- |
| `/` | 出现「航迹 · 航班运行分析」看板，不是 JSON |
| `/api/v1/system/health` | `{"code":0,...,"status":"ok"}` |
| 运行总览页 | KPI 卡片有数、趋势图有点、航线网络图有节点和连线 |
| 航班查询页 | 能翻页、能导出 CSV |
| 延误预测页 | 选 ORD → LAX，点预测，出概率与归因因子 |
| 数据管理页 | 未登录时只显示登录框（正常）；`admin/admin123` 可进 |

命令行复检：

```bash
curl https://<你的域名>/api/v1/system/health
curl "https://<你的域名>/api/v1/overview?date_from=2024-12-01&date_to=2024-12-31"
```

---

## 4. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面能开但图表全红 | 接口跨域或后端没起。确认 `/api/v1/system/health` 能返回 JSON，再看平台日志 |
| 打开很慢（30–60 s） | 免费档休眠后冷启动，属正常；答辩前先自己访问一次「热机」 |
| KPI 全 0 / 提示「数仓表尚无数据」 | `WAREHOUSE_ROOT` 没指到演示数仓。镜像里应为 `/app/data/demo/warehouse` |
| 预测页报「模型不存在」 | `MODEL_ROOT` 没指到 `/app/data/models`，或 `backend/data/models/*.pkl` 没进仓库 |
| `git push` 报 `remote: error: File ... is 105.00 MB` | GitHub 单文件上限 100 MB。演示数仓不应超过，检查是否误传了完整数仓 |
| Render 日志出现 `Killed` | 内存超 512 MB。确认 `QUERY_ENGINE=pandas`、没有加载整月 `dwd_flight_detail` |
| 想换成自己的完整数据 | 把 `WAREHOUSE_ROOT` 指向全量数仓目录（需自己找存储），演示数仓只读即可 |

---

## 5. 更新流程

```powershell
# 改完代码
git add .
git commit -m "fix: xxx"
git push            # GitHub 更新；Render/HF 自动重新构建
```

若改动了数仓或模型：

```powershell
cd backend
python scripts/export_demo_data.py --year 2024 --sample-rows 80000
cd ..
git add backend/data/demo backend/data/models
git commit -m "chore: 刷新演示数据"
git push
```
