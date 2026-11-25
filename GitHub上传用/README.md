# KOL 红人邮件系统

一个给别人发合作邀约邮件的小系统。导入红人名单 → 系统按国家/博主类型/语种/平台自动挑对模板 → 一键发送。

**一个人一封邮件逐封发送**，红人看不到其他人的邮箱。

## 特点

- **零依赖**：只用 Python 标准库，不用 `pip install` 任何东西。
- **跨平台**：Windows / macOS / Linux 都能跑，浏览器打开就能用。
- **沿用原版界面**：中文页签、顶部统计卡片、右侧发送操作与邮件预览。
- **能上云**：部署到云平台就是一个公开网址，评委/同事点开就能用，不用装任何软件。
- **会挑模板**：同一批名单里，日本复古数码博主收日语邮件，德国博主要德语邮件，系统自己匹配。

## 一、在自己电脑上打开

Windows 版本已经附带便携 Python，不需要安装软件或管理员权限：

- Windows：双击 `打开KOL系统.bat`（或 `start.bat`）
- macOS / Linux：需要 Python 3.10 以上，然后运行：

```bash
sh start.sh
```

然后浏览器会自动打开 `http://localhost:8787`。

同一局域网的同事，用 `http://<你的电脑IP>:8787` 也能打开。

## 二、变成一个公开网址（推荐给比赛用）

代码本身已经写成"任何机器都能跑"的形式，挑一种方式部署即可。

### 方式 1：Render 免费部署成公开网址（全程只用浏览器，什么都不用装）

办公电脑上没有 Python、没有 git 也不要紧，下面每一步都在浏览器里点。

#### 第 1 步：把代码放到 GitHub

1. 打开 <https://github.com/signup> 注册账号（已有账号直接登录）。
2. 打开 <https://github.com/new> 新建仓库：**Repository name** 填 `kol-mail-system`，选 **Public**（Private 也可以），点 **Create repository**。
3. 在新建好的仓库页面点 **uploading an existing file** 这个链接。
4. 打开电脑上的 `d:\kol` 文件夹，把下面这些**拖进浏览器的上传区域**（文件夹直接拖，目录结构会自动保留）：

   ```text
   server\            整个文件夹
   web\               整个文件夹
   data\              整个文件夹（里面是 7 套邮件模板）
   runtime\           整个文件夹（Windows 免安装运行环境，约 22 MB）
   render.yaml
   requirements.txt
   Dockerfile
   README.md
   start.bat  start.sh     可选，本地启动用
   ```

   **不要拖**这三个：`_archive\`、`legacy-powershell\`、`outputs\`。它们是老版本和历史文件，不需要公开。

5. 页面往下拉，点绿色的 **Commit changes**。

#### 第 2 步：在 Render 上创建服务

1. 打开 <https://dashboard.render.com/register>，选 **GitHub** 注册/登录（用 GitHub 账号登录，省得再记一套密码）。
2. 按提示授权 Render 读取你的仓库。
3. 点右上角 **New +** → **Web Service** → 找到 `kol-mail-system` → **Connect**。
4. Render 会自动读到仓库根目录的 `render.yaml`（名字、启动命令、免费套餐都配好了）。如果界面让你手填，就按这样填：

   | 项目 | 填什么 |
   | --- | --- |
   | Language / Runtime | `Python 3` |
   | Build Command | `true` |
   | Start Command | `python server/app.py` |
   | Instance Type | **Free** |

5. 点 **Deploy Web Service**，等 1～3 分钟（页面会滚动日志，最后出现 `Your service is live`）。

#### 第 3 步：拿到网址

服务页面顶部会显示一个 `https://kol-mail-system-xxxx.onrender.com` 的地址，这就是比赛用的公开网址。发给评委，他们用任何电脑或手机浏览器点开就能用，不用装任何东西。

#### 免费实例的两个特性（不知道的话会以为坏了）

- **会休眠**：15 分钟没人访问就自动休眠，再次打开需要约 1 分钟唤醒（浏览器会先显示 Render 的加载页），起来之后一切正常。
- **硬盘是临时的**：休眠或重新部署后，界面上改过的模板和发送记录会回到仓库里 `data/state.json` 的初始内容。想让模板长期生效，就在本地改好 `data/state.json` 再提交一次。

#### 安全提醒

公开网址意味着**任何拿到链接的人都能打开这个系统**。建议比赛演示结束后，在 Render 服务页面点 **Suspend**（需要时再 **Resume**），不要让它一直挂着。

### 方式 2：Docker（任意云主机 / 自己的服务器）

```bash
docker build -t kol-mail .
docker run -p 8787:8787 kol-mail
```

### 方式 3：任何支持 Python 的平台（Railway / Fly.io / 阿里云 / 腾讯云……）

启动命令统一是：

```bash
python server/app.py
```

平台一般会自动注入 `PORT` 环境变量，系统会自己读取。需要固定监听地址时用 `KOL_HOST`（默认 `0.0.0.0`）。

国内平台（如腾讯云云托管）配置步骤比 Render 多一些，需要你有对应云账号并完成实名，但国内访问更快，不受 Render 休眠影响。

## 三、名单表格怎么填

Excel / CSV 第一行写表头：

```text
姓名,邮箱,主页链接,平台,备注,物料单号,视频标题
```

认识的列名：

- 姓名：`name`、`姓名`、`红人姓名`
- 邮箱：`email`、`邮箱`
- 主页链接：`profileUrl`、`profile_url`、`主页链接`
- 平台：`platform`、`平台`
- 备注：`note`、`备注`

想加多少列都行（`国家`、`博主类型`、`语种`、`优惠码`……）。第一行写什么列名，模板里就能用对应占位符。
即使列名不叫"邮箱"，只要单元格里有 `alice@example.com` 这类邮箱，系统也会尽量认出来。

## 四、邮件模板

用 `{{name}}` 表示红人姓名，发送前自动替换。可以加粗：`**这段话会加粗**`。

```text
{{name}} 您好，

我很喜欢你最近的 {{视频标题}}。
这次合作的物料单号是 {{物料单号}}。

我们想和您聊一下产品合作。
```

如果邮件里还剩没替换的 `{{xxx}}`，系统会**拦截这一封**，不会发出去。

模板库存在 `data/state.json` 的 `templates` 里，每条模板可以带 `country` / `creatorType` / `language` / `platform` 四个匹配条件，系统按权重（博主类型 5 > 国家 3 > 语种 2 > 平台 1）打分挑最合适的一条。

## 五、安全设计

- 发件账号、授权码**不写进文件**，每次打开重新填，避免密码留在磁盘上。
- 强制**一封邮件只有一个收件人**，不允许抄送/密送，防止红人互相看到。
- 发送前自检：收件邮箱为空、邮箱里写了多个地址、标题为空、正文为空、正文变成乱码，都会拦截并写进日志。
- 有每日发送上限和每封之间的间隔，避免被判定为垃圾邮件。

## 六、目录说明

```text
start.bat / start.sh      启动脚本
打开KOL系统.bat           Windows 双击入口
runtime/python/           Windows 便携运行环境（免安装）
server/app.py             后端（HTTP 服务 + 名单导入 + 模板匹配 + 发信）
web/index.html            前端页面
data/state.json           模板库与设置
outputs/                  使用教程、名单模板示例
requirements.txt          空文件，只为让云平台识别成 Python 项目
Dockerfile / render.yaml  部署配置
legacy-powershell/        老版本（Windows 桌面版，PowerShell），保留备用
_archive/                 历史版本和被清理出来的旧文件，不参与提交
```

## 七、部署到公网要注意

公开网址意味着**任何拿到链接的人都能打开同一个系统**，看到同一份导入的名单和发送日志。比赛演示够用；如果要长期对外开放，建议加上登录口令，或者用 `KOL_HOST=127.0.0.1` 只给自己用。

免费云平台（如 Render 免费实例）休眠后数据会重置，模板库会回到 `data/state.json` 里的初始内容。
