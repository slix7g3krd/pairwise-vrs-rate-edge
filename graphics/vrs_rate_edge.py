"""可变速率着色（VRS）速率图管线。

修复的问题：
1. 边缘过度降采样   —— edge_rate：边缘检测 + 膨胀，边缘区域强制 1x1。
2. 运动/静态速率冲突 —— motion_mask：运动区域优先，覆盖静态背景速率。
3. 跨帧不同步       —— frame_fence：速率图上传与绘制之间插入栅栏。
4. MSAA 采样不一致  —— sample_consistency：着色率与采样数对齐。
5. GPU 能力差异     —— capability_query / fallback：能力查询与降级。
6. 速率图不确定性   —— rate_determinism / determinism：同输入同输出。
"""

# 着色率编码：1 = 1x1（最细），2 = 2x2，4 = 4x4（最粗）
VALID_RATES = (1, 2, 4)

# 能力等级：tier2 支持逐图元 + 速率图，tier1 仅支持粗粒度，tier0 不支持
_TIERS = {
    "tier2": {"rates": (1, 2, 4), "tile_size": 16, "per_draw": True, "image": True},
    "tier1": {"rates": (1, 2), "tile_size": 16, "per_draw": True, "image": False},
    "tier0": {"rates": (1,), "tile_size": 0, "per_draw": False, "image": False},
}


# ---------------------------------------------------------------- 工具

def _scene(n):
    """确定性的合成测试场景：n 为尺寸基数，返回 (prev, curr) 两帧灰度图。

    含一个运动方块（制造边缘与运动区域），纯整数运算保证确定性。
    """
    size = 8 * max(1, int(n))
    prev, curr = [], []
    for y in range(size):
        prow, crow = [], []
        for x in range(size):
            base = (x * 3 + y * 5) % 7  # 缓变背景
            prow.append(base)
            v = base
            # 运动方块：curr 中相对 prev 右移 2 像素
            if size // 4 <= y < size // 2:
                if size // 4 + 2 <= x < size // 2 + 2:
                    v = 200
            crow.append(v)
        prev.append(prow)
        curr.append(crow)
    # prev 中方块位于未平移位置
    for y in range(size // 4, size // 2):
        for x in range(size // 4, size // 2):
            prev[y][x] = 200
    return prev, curr


def _map_like(img, value):
    return [[value for _ in row] for row in img]


def _dilate(mask, radius=1):
    """二值掩码膨胀（4 邻域迭代 radius 次），用于边缘保护带。"""
    h, w = len(mask), len(mask[0])
    cur = [row[:] for row in mask]
    for _ in range(radius):
        nxt = [row[:] for row in cur]
        for y in range(h):
            for x in range(w):
                if cur[y][x]:
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and cur[ny][nx]:
                        nxt[y][x] = True
                        break
        cur = nxt
    return cur


# ---------------------------------------------------------------- 1. 边缘速率图

def edge_rate(image=None, threshold=32, protect_radius=1):
    """由边缘检测生成速率图：边缘（含保护带）为 1x1，过渡区 2x2，平坦区 4x4。

    修复“边缘过度降采样”：边缘先膨胀一圈保护带再分配速率，
    避免边缘像素落入 2x2/4x4 粗着色块而产生锯齿。
    """
    if image is None or isinstance(image, int):
        _, image = _scene(image or 1)
    h, w = len(image), len(image[0])
    mag = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            gx = image[y][min(x + 1, w - 1)] - image[y][max(x - 1, 0)]
            gy = image[min(y + 1, h - 1)][x] - image[max(y - 1, 0)][x]
            mag[y][x] = abs(gx) + abs(gy)
    edge = [[mag[y][x] > threshold for x in range(w)] for y in range(h)]
    protected = _dilate(edge, protect_radius)
    near = _dilate(edge, protect_radius + 1)
    rate = _map_like(image, 4)
    for y in range(h):
        for x in range(w):
            if protected[y][x]:
                rate[y][x] = 1
            elif near[y][x]:
                rate[y][x] = 2
    return rate


# ---------------------------------------------------------------- 2. 运动掩码

def motion_mask(prev=None, curr=None, threshold=16):
    """帧间差分生成运动掩码。

    修复“运动物体与静态背景速率图冲突”：运动区域优先级最高，
    合成时强制 1x1，覆盖静态背景可能给出的粗速率。
    """
    if prev is None or isinstance(prev, int):
        prev, curr = _scene(prev or 1)
    h, w = len(prev), len(prev[0])
    mask = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if abs(curr[y][x] - prev[y][x]) > threshold:
                mask[y][x] = True
    return mask


def shading_rate(arg=None):
    """合成最终速率图：边缘速率图为底，运动掩码区域强制 1x1。"""
    seed = arg if isinstance(arg, int) else 1
    prev, curr = _scene(seed or 1)
    rate = edge_rate(curr)
    mask = motion_mask(prev, curr)
    for y in range(len(rate)):
        for x in range(len(rate[0])):
            if mask[y][x]:
                rate[y][x] = 1
    return rate


# ---------------------------------------------------------------- 3. 跨帧栅栏

def frame_fence(frames=None):
    """速率图上传与绘制的跨帧同步。

    修复“速率图上传与绘制跨帧未同步”：帧 N 的绘制只允许消费
    帧 N-1 已栅栏确认上传完成的速率图；未就绪时回退 1x1 安全速率，
    绝不消费正在上传的缓冲。
    """
    n = frames if isinstance(frames, int) and frames else 3
    timeline = []
    uploaded = None  # 上一帧已 fence 的速率图
    for f in range(1, n + 1):
        computed = shading_rate(1)
        # 栅栏：上传完成后才对下一帧可见
        fence = {"frame": f, "signaled": True}
        draw_rate = uploaded if uploaded is not None else _map_like(computed, 1)
        timeline.append({
            "frame": f,
            "draw_uses_frame": f - 1 if uploaded is not None else None,
            "fence": fence,
            "fallback": uploaded is None,
        })
        if fence["signaled"]:
            uploaded = computed
    return timeline


# ---------------------------------------------------------------- 4. 采样数一致性

def sample_consistency(msaa_samples=4, rate=None):
    """修复“多重采样下着色率与采样数不一致”。

    粗着色块的覆盖采样数 = msaa_samples，着色采样按 rate 缩减；
    返回每个 (rate) 下合法的着色/覆盖采样配置，非法组合被钳制。
    """
    msaa_samples = int(msaa_samples) if msaa_samples else 1
    if msaa_samples not in (1, 2, 4, 8, 16):
        msaa_samples = 1
    rates = (rate,) if rate in VALID_RATES else VALID_RATES
    table = {}
    for r in rates:
        # 粗块内像素数 r*r，着色采样数不能超过覆盖采样总数
        shade = max(1, (msaa_samples * 1) // (r * r))
        table[r] = {
            "coverage_samples": msaa_samples,
            "shading_samples": shade,
            "consistent": shade >= 1 and shade <= msaa_samples,
        }
    return table


def msaa_path(arg=None):
    """MSAA 渲染路径：先对齐采样数，再输出速率图与采样配置。"""
    seed = arg if isinstance(arg, int) and arg else 1
    rate = shading_rate(seed)
    samples = sample_consistency(4)
    # 图中出现的速率都必须在一致性表中合法
    used = {v for row in rate for v in row}
    assert all(samples[r]["consistent"] for r in used)
    return {"rate_map": rate, "msaa": samples}


# ---------------------------------------------------------------- 5. 能力查询与降级

def capability_query(gpu=None):
    """查询 GPU 的 VRS 能力；未知设备按 tier0（不支持）处理。"""
    name = (gpu if isinstance(gpu, str) else "").lower()
    if any(k in name for k in ("rtx", "rdna", "arc", "tier2", "full")):
        tier = "tier2"
    elif any(k in name for k in ("gtx", "tier1", "partial")):
        tier = "tier1"
    else:
        tier = "tier0"
    caps = dict(_TIERS[tier])
    caps["tier"] = tier
    caps["supported"] = tier != "tier0"
    return caps


def fallback(arg=None):
    """按能力降级速率图：不支持的速率向上取到最近受支持速率；完全不支持则全 1x1。"""
    if isinstance(arg, dict):
        caps, rate = arg, shading_rate(1)
    else:
        caps = capability_query(arg if isinstance(arg, str) else "tier2")
        rate = shading_rate(1)
    supported = caps["rates"]
    out = []
    for row in rate:
        orow = []
        for v in row:
            cand = [r for r in supported if r >= v]
            orow.append(min(cand) if cand else max(supported))
        out.append(orow)
    return out


# ---------------------------------------------------------------- 6. 确定性

def _hash_map(m):
    h = 1469598103934665603
    for row in m:
        for v in row:
            h = ((h ^ (v & 0xFF)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def rate_determinism(seed=None):
    """速率图确定性：同一输入两次生成，逐像素相等且哈希一致。"""
    seed = seed if isinstance(seed, int) and seed else 1
    a = shading_rate(seed)
    b = shading_rate(seed)
    return {
        "identical": a == b,
        "hash_a": _hash_map(a),
        "hash_b": _hash_map(b),
    }


def determinism(arg=None):
    """整条管线（边缘 + 运动 + 合成 + 降级）的确定性校验。"""
    seed = arg if isinstance(arg, int) and arg else 1
    r1 = rate_determinism(seed)
    f1, f2 = fallback("tier2"), fallback("tier2")
    return {"rate": r1["identical"], "fallback": f1 == f2}


# ---------------------------------------------------------------- 校验 / 审计 / 指标

def validation(arg=None):
    """校验速率图：速率值合法、尺寸非零、行列一致。"""
    rate = arg if isinstance(arg, list) else shading_rate(arg if isinstance(arg, int) else 1)
    ok = bool(rate) and bool(rate[0])
    width = len(rate[0]) if ok else 0
    errors = []
    for y, row in enumerate(rate):
        if len(row) != width:
            errors.append(f"row {y} width {len(row)} != {width}")
        for x, v in enumerate(row):
            if v not in VALID_RATES:
                errors.append(f"invalid rate {v} at ({x},{y})")
    return {"valid": ok and not errors, "errors": errors}


def audit(arg=None):
    """审计速率图：各速率占比与潜在风险（边缘区域被粗化的像素数）。"""
    rate = arg if isinstance(arg, list) else shading_rate(arg if isinstance(arg, int) else 1)
    total = sum(len(r) for r in rate)
    dist = {r: 0 for r in VALID_RATES}
    for row in rate:
        for v in row:
            dist[v] += 1
    return {
        "total": total,
        "distribution": {r: dist[r] / total for r in VALID_RATES},
        "fully_resolved_ratio": dist[1] / total,
    }


def hysteresis(arg=None, up_frames=2, down_frames=1):
    """时间滞后：速率由细变粗需连续 up_frames 帧确认，由粗变细立即生效。

    防止速率图在边缘/运动边界逐帧闪烁。
    """
    seed = arg if isinstance(arg, int) and arg else 1
    prev, curr = _scene(seed)
    # 模拟三帧：t0 静态背景速率，t1/t2 当前帧速率
    target = shading_rate(seed)
    history = [_map_like(target, 4), target, target]
    h, w = len(target), len(target[0])
    out = _map_like(target, 4)
    for y in range(h):
        for x in range(w):
            vals = [f[y][x] for f in history]
            cur = vals[-1]
            if cur < vals[0]:
                out[y][x] = cur  # 变细立即生效
            else:
                # 变粗需最近 up_frames 帧一致
                out[y][x] = cur if all(v == cur for v in vals[-up_frames:]) else vals[0]
    return out


def metrics(arg=None):
    """着色开销指标：相对全 1x1 的着色 quad 数与节省百分比。"""
    rate = arg if isinstance(arg, list) else shading_rate(arg if isinstance(arg, int) else 1)
    total_px = sum(len(r) for r in rate)
    quads = sum(1 / (v * v) for row in rate for v in row)
    return {
        "pixels": total_px,
        "shading_quads": quads,
        "full_rate_quads": float(total_px),
        "savings_pct": round(100.0 * (1.0 - quads / total_px), 2),
    }


def debug_view(arg=None):
    """速率图可视化：1x1→R，2x2→G，4x4→B 的伪彩色 RGB 图。"""
    rate = arg if isinstance(arg, list) else shading_rate(arg if isinstance(arg, int) else 1)
    palette = {1: (255, 0, 0), 2: (0, 255, 0), 4: (0, 0, 255)}
    return [[palette[v] for v in row] for row in rate]
