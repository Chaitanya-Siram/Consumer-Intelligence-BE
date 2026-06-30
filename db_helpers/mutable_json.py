"""Deep-mutable JSON column type for SQLAlchemy.

`MutableDict.as_mutable(JSON)` only detects changes at the top level. Setting a
nested key (e.g. row.workflow["upload"]["source_file"] = ...) leaves the column
clean and commit() writes nothing. NestedMutableDict / NestedMutableList wrap
every nested dict/list so mutations at any depth bubble up to the parent ORM
attribute via `changed()`.

Usage:
    workflow = Column(NestedMutableDict.as_mutable(JSON), nullable=False)
"""
from typing import Any

from sqlalchemy.ext.mutable import Mutable


class NestedMutableDict(Mutable, dict):
    @classmethod
    def coerce(cls, key: str, value: Any) -> Any:
        # Accept both JSON object (dict) and JSON array (list) payloads so a single
        # column can hold either shape — e.g. `queries` may be a dict for one flow
        # and a list of query groups for another. Lists are coerced to the sibling
        # NestedMutableList so nested mutations still bubble up to the ORM attribute.
        if value is None or isinstance(value, (NestedMutableDict, NestedMutableList)):
            return value
        if isinstance(value, dict):
            return NestedMutableDict(value)
        if isinstance(value, list):
            return NestedMutableList(value)
        return Mutable.coerce(key, value)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for k, v in list(self.items()):
            dict.__setitem__(self, k, _wrap(v, self))

    def __setitem__(self, key: Any, value: Any) -> None:
        dict.__setitem__(self, key, _wrap(value, self))
        self.changed()

    def __delitem__(self, key: Any) -> None:
        dict.__delitem__(self, key)
        self.changed()

    def update(self, *args: Any, **kwargs: Any) -> None:
        for k, v in dict(*args, **kwargs).items():
            self[k] = v  # routes through __setitem__ → wrap + changed

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        value = dict.pop(self, *args, **kwargs)
        self.changed()
        return value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
            return self[key]
        return self[key]


class NestedMutableList(Mutable, list):
    @classmethod
    def coerce(cls, key: str, value: Any) -> Any:
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, list):
            return cls(value)
        return Mutable.coerce(key, value)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for i, v in enumerate(list(self)):
            list.__setitem__(self, i, _wrap(v, self))

    def __setitem__(self, index: Any, value: Any) -> None:
        list.__setitem__(self, index, _wrap(value, self))
        self.changed()

    def __delitem__(self, index: Any) -> None:
        list.__delitem__(self, index)
        self.changed()

    def append(self, value: Any) -> None:
        list.append(self, _wrap(value, self))
        self.changed()

    def extend(self, values: Any) -> None:
        list.extend(self, [_wrap(v, self) for v in values])
        self.changed()

    def insert(self, index: int, value: Any) -> None:
        list.insert(self, index, _wrap(value, self))
        self.changed()

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        value = list.pop(self, *args, **kwargs)
        self.changed()
        return value

    def remove(self, value: Any) -> None:
        list.remove(self, value)
        self.changed()

    def clear(self) -> None:
        list.clear(self)
        self.changed()


def _wrap(value: Any, parent: Mutable) -> Any:
    """Recursively wrap dicts and lists so nested writes flow back to `parent`."""
    if isinstance(value, dict) and not isinstance(value, NestedMutableDict):
        wrapped = NestedMutableDict(value)
        wrapped._parents = parent._parents  # share parent registry
        return wrapped
    if isinstance(value, list) and not isinstance(value, NestedMutableList):
        wrapped = NestedMutableList(value)
        wrapped._parents = parent._parents
        return wrapped
    return value
