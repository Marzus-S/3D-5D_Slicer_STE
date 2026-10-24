# Анализ геометрии модели на numpy: нависания, площадь опоры, тело вращения, центральное отверстие.
# Координаты сцены Uranium: ось Y направлена вверх. Для пользователя переводим в координаты принтера
# (X, Y, Z), где Z — высота: printer = (scene.x, -scene.z, scene.y).

import math

import numpy

UP = numpy.array([0.0, 1.0, 0.0])
DEFAULT_OVERHANG_ANGLE = 45.0


def to_printer(vector, digits=3):
    return [round(float(vector[0]), digits), round(float(-vector[2]), digits), round(float(vector[1]), digits)]


def size_to_printer(size, digits=2):
    return {"x": round(float(size[0]), digits), "y": round(float(size[2]), digits), "z": round(float(size[1]), digits)}


def triangles_from_mesh(vertices, indices=None):
    vertices = numpy.asarray(vertices, dtype=numpy.float64)
    if indices is not None and len(indices) > 0:
        return vertices[numpy.asarray(indices).reshape(-1, 3)]
    count = (len(vertices) // 3) * 3
    return vertices[:count].reshape(-1, 3, 3)


def face_data(triangles):
    cross = numpy.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    norm = numpy.sqrt((cross ** 2).sum(axis=1))
    valid = norm > 1e-12
    triangles = triangles[valid]
    normals = cross[valid] / norm[valid][:, None]
    areas = norm[valid] * 0.5
    return triangles, normals, areas


def orientation_metrics(triangles, normals, areas, down, overhang_angle=DEFAULT_OVERHANG_ANGLE):
    """Метрики печати, если модель положить так, чтобы вектор down смотрел в стол."""
    down = numpy.asarray(down, dtype=numpy.float64)
    down = down / numpy.linalg.norm(down)
    up = -down
    heights = triangles.dot(up)
    h_min = heights.min()
    face_min = heights.min(axis=1)
    face_max = heights.max(axis=1)
    cos_up = normals.dot(up)

    near_bed = face_min <= h_min + 0.3
    on_bed = face_max <= h_min + 0.05
    overhang = (cos_up < -math.sin(math.radians(overhang_angle))) & ~near_bed
    contact = on_bed & (cos_up < -0.99)

    total = float(areas.sum())
    overhang_area = float(areas[overhang].sum())
    return {
        "down": down,
        "overhang_area": overhang_area,
        "overhang_percent": 100.0 * overhang_area / total if total else 0.0,
        "contact_area": float(areas[contact].sum()),
        "height": float(heights.max() - h_min),
    }


def candidate_downs(normals, areas, max_faces=8):
    """Кандидаты «какой стороной положить»: 6 осей + нормали крупнейших плоских граней."""
    quantized = numpy.round(normals * 20).astype(numpy.int64) + 20
    codes = (quantized[:, 0] * 41 + quantized[:, 1]) * 41 + quantized[:, 2]
    _, inverse = numpy.unique(codes, return_inverse=True)
    sums = numpy.bincount(inverse, weights=areas)
    candidates = []
    for group in numpy.argsort(-sums)[:max_faces]:
        mask = inverse == group
        direction = (normals[mask] * areas[mask][:, None]).sum(axis=0)
        length = numpy.linalg.norm(direction)
        if length > 1e-9:
            candidates.append(direction / length)
    for axis in numpy.eye(3):
        candidates.append(axis)
        candidates.append(-axis)

    unique = []
    for c in candidates:
        if all(float(c.dot(u)) < 0.999 for u in unique):
            unique.append(c)
    return unique


def _line_hits(triangles, origin, direction):
    """Сколько треугольников пересекает бесконечная прямая (Möller–Trumbore без ограничения по t)."""
    v0 = triangles[:, 0]
    e1 = triangles[:, 1] - v0
    e2 = triangles[:, 2] - v0
    p = numpy.cross(direction, e2)
    det = (e1 * p).sum(axis=1)
    ok = numpy.abs(det) > 1e-12
    inv = numpy.zeros_like(det)
    inv[ok] = 1.0 / det[ok]
    s = origin - v0
    u = (s * p).sum(axis=1) * inv
    q = numpy.cross(s, e1)
    v = q.dot(direction) * inv
    hit = ok & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0)
    return int(hit.sum())


def _face_normals(triangles):
    cross = numpy.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    norm = numpy.sqrt((cross ** 2).sum(axis=1))
    norm[norm < 1e-12] = 1.0
    return cross / norm[:, None]


def _symmetry_about(relative, axis):
    """Насколько одинаков разброс точек в двух направлениях, перпендикулярных оси."""
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(axis, helper)
    u /= numpy.linalg.norm(u)
    v = numpy.cross(axis, u)
    a, b = float(relative.dot(u).var()), float(relative.dot(v).var())
    return min(a, b) / max(a, b, 1e-12)


def _radial_profile(relative, axis):
    """Положение вдоль оси, радиус от оси и «круглость» контура (min/max радиуса по 36 секторам)."""
    along = relative.dot(axis)
    radial = relative - numpy.outer(along, axis)
    radius = numpy.sqrt((radial ** 2).sum(axis=1))
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(axis, helper)
    u /= numpy.linalg.norm(u)
    v = numpy.cross(axis, u)
    angles = numpy.arctan2(radial.dot(v), radial.dot(u))
    bins = ((angles + math.pi) / (2 * math.pi) * 36).astype(numpy.int64) % 36
    max_per_bin = numpy.zeros(36)
    numpy.maximum.at(max_per_bin, bins, radius)
    roundness = float(max_per_bin.min() / max_per_bin.max()) if max_per_bin.max() > 0 else 0.0
    return along, radius, roundness


def revolution_info(triangles, areas):
    """Похожа ли деталь на тело вращения, где ось, есть ли сквозное центральное отверстие."""
    centers = triangles.mean(axis=1)
    weights = areas / areas.sum()
    mean = (centers * weights[:, None]).sum(axis=0)
    delta = centers - mean
    covariance = (delta * weights[:, None]).T.dot(delta)
    values, vectors = numpy.linalg.eigh(covariance)
    points = triangles.reshape(-1, 3)
    relative = points - mean

    # Кандидаты в оси: главные оси инерции и оси координат (модели обычно выровнены по осям).
    # Оценка = «круглость» контура + доля поверхности, параллельной оси (цилиндры, стенки кулачков).
    candidates = []
    for i in range(3):
        j, k = [x for x in range(3) if x != i]
        symmetry = min(values[j], values[k]) / max(values[j], values[k], 1e-12)
        candidates.append((vectors[:, i] / numpy.linalg.norm(vectors[:, i]), symmetry))
    for unit in numpy.eye(3):
        if all(abs(float(unit.dot(c[0]))) < 0.995 for c in candidates):
            candidates.append((unit, None))
    normals = _face_normals(triangles)
    total_area = float(areas.sum())
    best = None
    for candidate, symmetry in candidates:
        along, radius, roundness = _radial_profile(relative, candidate)
        parallel = float(areas[numpy.abs(normals.dot(candidate)) < 0.15].sum()) / total_area
        if symmetry is None:
            symmetry = _symmetry_about(relative, candidate)
        score = roundness + parallel
        if best is None or score > best[0]:
            best = (score, candidate, symmetry, along, radius, roundness, parallel)
    _, axis, best_symmetry, along, radius, roundness, parallel_share = best

    through_bore = _line_hits(triangles, mean, axis) == 0
    is_revolution = best_symmetry > 0.85 and roundness > 0.9
    return {
        "axis": axis,
        "center": mean,
        "symmetry": float(best_symmetry),
        "roundness": roundness,
        "axis_parallel_share": parallel_share,
        "is_body_of_revolution": bool(is_revolution),
        "axis_is_vertical": bool(abs(float(axis.dot(UP))) > 0.98),
        "outer_diameter": float(2.0 * radius.max()),
        "length_along_axis": float(along.max() - along.min()),
        "through_bore": bool(through_bore),
        "bore_diameter": float(2.0 * radius.min()) if through_bore else 0.0,
    }


def mesh_health(triangles, max_triangles_for_bodies=2000000):
    """Целостность сетки: открытые рёбра (дыры), рёбра с тремя и больше гранями, число отдельных тел.
    STL хранит каждый треугольник отдельно, поэтому вершины сначала «свариваем» по координатам."""
    points = numpy.round(triangles.reshape(-1, 3) / 1e-4).astype(numpy.int64)
    _, ids = numpy.unique(points, axis=0, return_inverse=True)
    ids = ids.reshape(-1, 3)
    edges = numpy.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
    edges.sort(axis=1)
    _, counts = numpy.unique(edges, axis=0, return_counts=True)
    result = {
        "open_edges": int((counts == 1).sum()),
        "nonmanifold_edges": int((counts > 2).sum()),
        "bodies": None,
    }
    result["watertight"] = result["open_edges"] == 0 and result["nonmanifold_edges"] == 0
    if len(triangles) <= max_triangles_for_bodies:
        result["bodies"] = _component_count(int(ids.max()) + 1, edges)
    return result


def _component_count(count, edges):
    """Число связных компонент: «подвешивание» к меньшему номеру + сжатие путей (за log шагов, без Python-циклов по рёбрам)."""
    parent = numpy.arange(count)
    a, b = edges[:, 0], edges[:, 1]
    for _ in range(64):
        pa, pb = parent[a], parent[b]
        if (pa == pb).all():
            break
        numpy.minimum.at(parent, numpy.maximum(pa, pb), numpy.minimum(pa, pb))
        while True:
            jumped = parent[parent]
            if (jumped == parent).all():
                break
            parent = jumped
    return int(len(numpy.unique(parent)))


def wall_thickness(triangles, normals, areas, samples=200, limit=5.0, seed=0):
    """Толщина стенки в случайных точках поверхности (с учётом площади): луч от грани внутрь до
    противоположной стенки. Возвращает массив толщин; inf — стенка толще limit или луч не нашёл выхода."""
    if len(triangles) == 0:
        return numpy.zeros(0)
    random = numpy.random.RandomState(seed)
    picked = random.choice(len(triangles), size=min(samples, len(triangles)), p=areas / areas.sum())
    centers = triangles.mean(axis=1)
    reach = numpy.sqrt(((triangles - centers[:, None, :]) ** 2).sum(axis=2)).max(axis=1)
    result = numpy.full(len(picked), numpy.inf)
    for n, face in enumerate(picked):
        direction = -normals[face]
        origin = centers[face] + direction * 1e-3
        # Только грани, до которых от точки не дальше limit: остальные заведомо не дадут тонкую стенку.
        near = numpy.sqrt(((centers - origin) ** 2).sum(axis=1)) - reach <= limit
        near[face] = False
        hits = _ray_distances(triangles[near], origin, direction)
        hits = hits[(hits > 1e-3) & (hits <= limit)]
        if len(hits):
            result[n] = float(hits.min()) + 1e-3
    return result


def _ray_distances(triangles, origin, direction):
    """Расстояния вдоль луча до пересекаемых треугольников (Möller–Trumbore); без пересечения — -1."""
    if len(triangles) == 0:
        return numpy.zeros(0)
    v0 = triangles[:, 0]
    e1 = triangles[:, 1] - v0
    e2 = triangles[:, 2] - v0
    p = numpy.cross(direction, e2)
    det = (e1 * p).sum(axis=1)
    ok = numpy.abs(det) > 1e-12
    inv = numpy.zeros_like(det)
    inv[ok] = 1.0 / det[ok]
    s = origin - v0
    u = (s * p).sum(axis=1) * inv
    q = numpy.cross(s, e1)
    v = q.dot(direction) * inv
    t = (e2 * q).sum(axis=1) * inv
    hit = ok & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0)
    return numpy.where(hit, t, -1.0)


def _radial(points, center, axis):
    relative = points - center
    radial = relative - numpy.outer(relative.dot(axis), axis)
    return radial, numpy.sqrt((radial ** 2).sum(axis=1))


def volume_outside_radius(triangles, normals, areas, axis, center, radius):
    """Объём материала дальше radius от оси. Теорема Гаусса–Остроградского для поля
    F = (r² − R²) / (2r) · r̂ при r > R и 0 внутри: div F = 1 снаружи цилиндра, поэтому поток F через
    поверхность детали равен объёму вне цилиндра. Интеграл по грани — по серединам рёбер."""
    total = 0.0
    for i, j in ((0, 1), (1, 2), (2, 0)):
        midpoints = (triangles[:, i] + triangles[:, j]) * 0.5
        radial, r = _radial(midpoints, center, axis)
        safe = numpy.maximum(r, 1e-9)
        field = numpy.where(r > radius, (r ** 2 - radius ** 2) / (2.0 * safe ** 2), 0.0)
        total += float(((radial * normals).sum(axis=1) * field * areas).sum())
    return abs(total) / 3.0


def radial_overhang_share(triangles, normals, areas, axis, center, start_radius, overhang_angle=DEFAULT_OVERHANG_ANGLE):
    """Доля поверхности, которой в 5D нужна поддержка: в цилиндрических слоях деталь растёт от оси наружу,
    и «нависают» грани, смотрящие к оси (как в 3D — грани, смотрящие вниз). Грани у базы не считаем."""
    radial, r = _radial(triangles.mean(axis=1), center, axis)
    unit = radial / numpy.maximum(r, 1e-9)[:, None]
    facing_axis = (normals * unit).sum(axis=1) < -math.sin(math.radians(overhang_angle))
    mask = facing_axis & (r > start_radius + 0.5)
    total = float(areas.sum())
    return 100.0 * float(areas[mask].sum()) / total if total else 0.0


def trapped_overhang_share(triangles, normals, areas, down, axis, overhang_angle=DEFAULT_OVERHANG_ANGLE,
                           samples=200, seed=0):
    """Доля поверхности (%), которой в 3D-ориентации down нужна поддержка, стоящая на самой детали
    (луч вниз упирается в деталь: каналы, полости между дисками), и при этом грань смотрит вдоль оси —
    в цилиндрических слоях 5D такая грань идёт как стенка и поддержки не требует."""
    down = numpy.asarray(down, dtype=numpy.float64)
    down = down / numpy.linalg.norm(down)
    heights = triangles.dot(-down)
    near_bed = heights.min(axis=1) <= heights.min() + 0.3
    overhang = (normals.dot(-down) < -math.sin(math.radians(overhang_angle))) & ~near_bed
    overhang &= numpy.abs(normals.dot(axis)) >= math.cos(math.radians(30))
    candidates = numpy.nonzero(overhang)[0]
    total = float(areas.sum())
    if len(candidates) == 0 or not total:
        return 0.0
    random = numpy.random.RandomState(seed)
    weights = areas[candidates] / areas[candidates].sum()
    picked = random.choice(candidates, size=min(samples, len(candidates)), p=weights)
    # Луч идёт вдоль down: отбираем треугольники, чья проекция на плоскость, перпендикулярную лучу, накрывает точку.
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(down[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(down, helper)
    u /= numpy.linalg.norm(u)
    v = numpy.cross(down, u)
    tu, tv = triangles.dot(u), triangles.dot(v)
    lo_u, hi_u, lo_v, hi_v = tu.min(axis=1), tu.max(axis=1), tv.min(axis=1), tv.max(axis=1)
    centers = triangles.mean(axis=1)
    trapped = 0
    for face in picked:
        origin = centers[face]
        pu, pv = float(origin.dot(u)), float(origin.dot(v))
        mask = (lo_u <= pu) & (hi_u >= pu) & (lo_v <= pv) & (hi_v >= pv)
        mask[face] = False
        hits = _ray_distances(triangles[mask], origin, down)
        if (hits > 1e-3).any():
            trapped += 1
    return 100.0 * float(areas[candidates].sum()) / total * trapped / len(picked)


def analyze(vertices, indices=None, overhang_angle=DEFAULT_OVERHANG_ANGLE):
    triangles = triangles_from_mesh(vertices, indices)
    triangles, normals, areas = face_data(triangles)
    if len(triangles) == 0:
        raise ValueError("Пустая или вырожденная сетка")

    points = triangles.reshape(-1, 3)
    size = points.max(axis=0) - points.min(axis=0)
    signed = float(numpy.einsum("ij,ij->i", triangles[:, 0], numpy.cross(triangles[:, 1], triangles[:, 2])).sum()) / 6.0
    if signed < 0:
        normals = -normals  # сетка вывернута (или зеркальный масштаб): нормали должны смотреть наружу
    volume = abs(signed)

    current = orientation_metrics(triangles, normals, areas, -UP, overhang_angle)
    candidates = [orientation_metrics(triangles, normals, areas, d, overhang_angle) for d in candidate_downs(normals, areas)]
    # Нависания сравниваем «корзинами» по 1 %, внутри корзины — больше площадь опоры и ниже высота.
    by_supports = sorted(candidates, key=lambda m: (round(m["overhang_percent"]), -m["contact_area"], m["height"]))
    by_contact = sorted(candidates, key=lambda m: (-m["contact_area"], m["overhang_area"]))

    return {
        "triangles": triangles,
        "normals": normals,
        "areas": areas,
        "triangle_count": int(len(triangles)),
        "size": size,
        "volume_mm3": volume,
        "inverted": signed < 0,
        "surface_mm2": float(areas.sum()),
        "current": current,
        "by_supports": by_supports,
        "by_contact": by_contact,
        "revolution": revolution_info(triangles, areas),
    }


def format_metrics(metrics):
    return {
        "down_vector": to_printer(metrics["down"]),
        "overhang_percent": round(metrics["overhang_percent"], 1),
        "overhang_area_mm2": round(metrics["overhang_area"], 1),
        "bed_contact_mm2": round(metrics["contact_area"], 1),
        "height_mm": round(metrics["height"], 2),
    }
