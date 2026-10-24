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


def analyze(vertices, indices=None, overhang_angle=DEFAULT_OVERHANG_ANGLE):
    triangles = triangles_from_mesh(vertices, indices)
    triangles, normals, areas = face_data(triangles)
    if len(triangles) == 0:
        raise ValueError("Пустая или вырожденная сетка")

    points = triangles.reshape(-1, 3)
    size = points.max(axis=0) - points.min(axis=0)
    volume = abs(float(numpy.einsum("ij,ij->i", triangles[:, 0], numpy.cross(triangles[:, 1], triangles[:, 2])).sum())) / 6.0

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
