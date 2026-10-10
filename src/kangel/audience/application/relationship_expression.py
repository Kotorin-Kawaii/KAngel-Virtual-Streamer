"""One read-only interpretation of relationships for replies and greetings."""
from collections.abc import Mapping


def relationship_expression(relationship) -> dict:
    data = relationship or {}
    def value(name, default):
        return data.get(name, default) if isinstance(data, Mapping) else getattr(data, name, default)
    familiarity = float(value("familiarity", .05))
    affinity = float(value("affinity", .5))
    trust = float(value("trust", .5))
    interactions = int(value("interaction_count", 0))
    known = interactions > 0 and familiarity >= .3
    level = "trusted" if known and familiarity >= .7 else "familiar" if known else "new"
    warmth = ("reserved" if affinity <= .35 or trust <= .35 else
              "warm" if known and affinity >= .7 and trust >= .6 else "normal")
    return {"level": level, "warmth": warmth,
            "evidence_scope": "trusted" if level == "trusted" and trust >= .65 else
                              "familiar" if known else "public"}
