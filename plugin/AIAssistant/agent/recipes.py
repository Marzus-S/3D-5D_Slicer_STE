# Рецепты: удачный набор решений (режим, материал, профиль, ориентация, ключевые настройки) под именем.
# Применение рецепта — тот же apply_plan, но без раундов LLM. Хранятся одним JSON рядом с историей чатов.

import json
import os
import re
import threading
import time


def _key(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


class RecipeStore:
    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._recipes = {}
        try:
            with open(path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                self._recipes = {_key(k): v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self):
        directory = os.path.dirname(self._path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        temporary = self._path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(self._recipes, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, self._path)

    def put(self, recipe):
        name = str(recipe.get("name") or "").strip()
        if not name:
            raise ValueError("У рецепта должно быть имя")
        with self._lock:
            old = self._recipes.get(_key(name)) or {}
            recipe = dict(recipe, name=name, created=old.get("created") or time.time(), used=old.get("used", 0))
            self._recipes[_key(name)] = recipe
            self._save()
        return recipe

    def get(self, name):
        """Точное имя или однозначное начало/часть имени."""
        with self._lock:
            wanted = _key(name)
            if wanted in self._recipes:
                return self._recipes[wanted]
            partial = [r for k, r in self._recipes.items() if wanted and wanted in k]
            if len(partial) == 1:
                return partial[0]
        names = ", ".join(r["name"] for r in partial) if partial else ", ".join(self.names()) or "рецептов пока нет"
        raise ValueError("Рецепт «%s» не найден однозначно. Есть: %s" % (name, names))

    def mark_used(self, name):
        with self._lock:
            recipe = self._recipes.get(_key(name))
            if recipe is not None:
                recipe["used"] = recipe.get("used", 0) + 1
                self._save()

    def delete(self, name):
        with self._lock:
            removed = self._recipes.pop(_key(name), None)
            if removed is not None:
                self._save()
        return removed is not None

    def names(self):
        with self._lock:
            return [r["name"] for r in self._recipes.values()]

    def all(self):
        with self._lock:
            return sorted(self._recipes.values(), key=lambda r: (-r.get("used", 0), r["name"].lower()))
