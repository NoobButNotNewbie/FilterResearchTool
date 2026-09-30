import re


class Taxonomy:
    def __init__(self, config: dict):
        tax_config = config.get("taxonomy", {})
        
        self.attack_methods = self._load_dimension(tax_config.get("attack_methods", {}))
        self.attack_stages = self._load_dimension(tax_config.get("attack_stages", {}))
        self.optimization_techniques = self._load_dimension(tax_config.get("optimization_techniques", {}))
        self.optimization_objectives = self._load_dimension(tax_config.get("optimization_objectives", {}))
        
    def _load_dimension(self, dim_config: dict) -> dict[str, list[str]]:
        result = {}
        for name, data in dim_config.items():
            keywords = data.get("keywords", [])
            result[name] = [k.lower() for k in keywords]
        return result
        
    def classify_text(self, text: str) -> dict:
        text_lower = text.lower()
        
        techniques = self._match(text_lower, self.optimization_techniques)
        objectives = self._match(text_lower, self.optimization_objectives)
        return {
            "attack_methods": self._match(text_lower, self.attack_methods),
            "attack_stages": self._match(text_lower, self.attack_stages),
            "optimization_techniques": techniques or ["none_detected"],
            "optimization_objectives": objectives or ["other_improvement"],
        }
        
    def _match(self, text: str, dimension: dict[str, list[str]]) -> list[str]:
        matches = []
        for name, keywords in dimension.items():
            for kw in keywords:
                if len(kw) <= 3:
                    matched = re.search(rf"\b{re.escape(kw)}\b", text) is not None
                else:
                    matched = kw in text
                if matched:
                    matches.append(name)
                    break # One match per category is enough
        return matches
        
    def get_all_categories(self) -> dict[str, list[str]]:
        return {
            "attack_methods": list(self.attack_methods.keys()),
            "attack_stages": list(self.attack_stages.keys()),
            "optimization_techniques": list(self.optimization_techniques.keys()) + ["none_detected"],
            "optimization_objectives": list(self.optimization_objectives.keys()) + ["other_improvement"]
        }
