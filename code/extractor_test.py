import sqlglot
from sqlglot import exp
import json
import os
from dataclasses import dataclass, field, asdict
from sqlglot.optimizer.scope import find_all_in_scope

@dataclass
class Tables:
    type: str
    alias: str
    level: str
    name: str
    global_path: str

@dataclass
class Mains:
    tables: dict[str, Tables] = field(default_factory=dict)
    joins: list = field(default_factory=list)

@dataclass
class SelectBlock:
    block_name: str
    tables: dict[str, Tables] = field(default_factory=dict)
    joins: list = field(default_factory=list)
    column_lineage: dict = field(default_factory=dict)

@dataclass
class Objects:
    alias_name: str
    obj_type: str
    parent: str
    select_blocks: list[SelectBlock] = field(default_factory=list)

@dataclass
class SemanticJSON:
    metadata: dict = field(default_factory=dict) # Думаю буду использовать Jira/Confluence
    objects: dict[str, Objects] = field(default_factory=dict)
    mains: dict[str, Mains] = field(default_factory=dict)


class SemanticExtractor:
    
    def extract(self, sql: str, save_path ='output\\data.json'):
        self.json_path = save_path
        
        ast = sqlglot.parse_one(sql, dialect="postgres") # AST(Abstract  Syntax Tree) - это дерево которое полностью повторяет SQL сиентакс
                
        semantic = SemanticJSON()

        other_query = ast.copy()
        ast_other = other_query.args.get("with_")

        if ast_other:
            self.extract_cte(ast_other, semantic)
        
        return semantic
    
    def lower_case(self, t):
        return str(t).lower()

    # Extract Union - вытаскивает каждый union select по отдельности
    def extract_union_selects(self, node):
        if isinstance(node, exp.Union):
            yield from self.extract_union_selects(node.this)
            yield from self.extract_union_selects(node.expression)
        else:
            yield node
            
    def get_type_of_table(self, ast):
        ttype = ''

        if isinstance(ast, exp.Table) and ast.db == '':
            ttype = 'cte'
        elif isinstance(ast, exp.Table) and ast.db != '':
            ttype = 'table'
        elif isinstance(ast, exp.Subquery):
            ttype = 'subquery'

        return ttype
    

    def extract_objects(self, ast, semantic, ttype, level):
        if ttype == 'subquery':
            alias_name = ast.alias
            semantic.tables[alias_name] = {
                "type": ttype,
                "alias": alias_name,
                "level": level,
                "name": f"{level}-{ttype}-{alias_name}",
                "global_path": f"{level}-{ttype}-{alias_name}"
            }
        elif ttype == 'cte':
            alias_name = ast.alias
            name = ast.name
            semantic.tables[alias_name] = {
                "type": ttype,
                "alias": alias_name,
                "level": level,
                "name": f"main-{ttype}-{name}",
                "global_path": f"main-{ttype}-{name}"
            }
        elif ttype == 'table':
            alias_name = ast.alias
            semantic.tables[alias_name] = {
                "type": ttype,
                "alias": alias_name,
                "level": level,
                "name": ".".join(
                    x for x in [ast.catalog, self.lower_case(ast.db), self.lower_case(ast.name)] if x
                ),
                "global_path": f"{level}"
            }
    
    
    def extract_from(self, ast, semantic, level):
        ast_from = ast.find(exp.From)

        if ast_from is None:
            print(f"[WARNING] FROM не найден: {type(ast).__name__}")
            print(ast)
            return

        ast_from = ast_from.this

        if ast_from is None:
            print(f"[WARNING] FROM.this пустой: {type(ast).__name__}")
            print(ast)
            return
        
        ttype = self.get_type_of_table(ast_from)

        self.extract_objects(ast_from, semantic, ttype, level)
    
    
    def extract_cte(self, ast, semantic, level="main"):
        # Вытаскиваем все cte не заходя дальше в глубину
        ctes = list(find_all_in_scope(ast, exp.CTE))
        
        
        if not ctes:
            return
        
        for cte in ctes:            
            key = f"{level}-cte-{cte.alias}"
            semantic.objects[key] = Objects(alias_name=cte.alias, obj_type='cte', parent=level)
            
            selects = list(self.extract_union_selects(cte.this))
            
            
            for i in range(len(selects)):
                print(f"{i}: {selects[i]}")
                
                query = selects[i]
                query.set("where", None)
                
                name = f'select_{i+1}'
                cte_level = f"{key}:{name}"

                semantic.objects[key].select_blocks.append(SelectBlock(block_name=name))
                
                new_cte_level = cte.alias
                self.extract_cte(query, semantic, new_cte_level)
                
                self.extract_from(query, semantic.objects[key].select_blocks[i], cte_level)
                
                
            
        
        print("Done")
    