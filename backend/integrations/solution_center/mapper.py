"""Kategori mapper: flat liste → ağaç (tree).

CM API kategorileri düz liste döndürür (her kayıt parentUuid ile ebeveynine
bağlı). Chatbot ağaç halinde gezdirip yalnızca isLeaf==true kategorilerin
seçilmesine izin verir (SPEC "Mapper").
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .models import Category, CategoryNode


def parse_categories(raw: Iterable[dict]) -> List[Category]:
    """Ham API kayıtlarını Category modellerine çevirir (bozuk kayıtları atlar)."""
    result: List[Category] = []
    for item in raw:
        try:
            result.append(Category.model_validate(item))
        except Exception:
            # Tek bir bozuk kayıt tüm listeyi düşürmesin.
            continue
    return result


def build_tree(categories: List[Category]) -> List[CategoryNode]:
    """Flat Category listesini kök düğümlerin listesine (ağaç) dönüştürür.

    - parentUuid==None olanlar kök.
    - Bilinmeyen parent'a işaret eden kayıtlar (yetim) köke terfi eder ki
      hiçbir kategori sessizce kaybolmasın.
    - Her seviye sortOrder'a göre sıralanır.
    """
    nodes: Dict[str, CategoryNode] = {
        c.uuid: CategoryNode(**c.model_dump(by_alias=False)) for c in categories
    }
    roots: List[CategoryNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_uuid) if node.parent_uuid else None
        if parent is not None and parent is not node:
            parent.children.append(node)
        else:
            roots.append(node)

    def _sort(items: List[CategoryNode]) -> None:
        items.sort(key=lambda n: (n.sort_order, n.name))
        for it in items:
            _sort(it.children)

    _sort(roots)
    return roots


def find_by_short_code(categories: List[Category], short_code: str) -> Optional[Category]:
    """shortCode ile kategoriyi bulur (flat liste üzerinde)."""
    for c in categories:
        if c.short_code == short_code:
            return c
    return None


def is_selectable(categories: List[Category], short_code: str) -> bool:
    """Kategori seçilebilir mi: var VE yaprak (isLeaf==true) olmalı."""
    cat = find_by_short_code(categories, short_code)
    return cat is not None and cat.is_leaf
