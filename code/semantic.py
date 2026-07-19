from dataclasses import dataclass, field, asdict


@dataclass
class SemanticJSON:
    
    metadata: dict = field(default_factory=dict) # Думаю буду использовать Jira/Confluence

    tables: list = field(default_factory=list)

    joins: list = field(default_factory=list)