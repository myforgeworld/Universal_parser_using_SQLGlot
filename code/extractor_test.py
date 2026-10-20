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
            
        main_query = ast.copy()
        main_query.args.pop("with_", None) # Удаляем все кроме основного запроса
        level = 'main'

        selects = list(self.extract_union_selects(main_query))
        
        for i in range(len(selects)):
            name = f'main_{i+1}'
            query = selects[i]
            level = name

            semantic.mains[name] = Mains()

            self.extract_subquery(query, semantic, level)
            self.extract_from(query, semantic.mains[name], semantic, level)
            self.extract_joins(query, semantic.mains[name], semantic, level)
        
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
    
    def get_join_source(self, node, semantic, semantic_master, level):
        ttype = self.get_type_of_table(node)
        
        return self.extract_objects(node, semantic, semantic_master, ttype, level)
    

    def extract_objects(self, ast, semantic, semantic_master, ttype, level):
        if ttype == 'subquery':
            
            self.extract_subquery(ast, semantic_master, level)
            
            alias_name = ast.alias
            semantic.tables[alias_name] = {
                "type": ttype,
                "alias": alias_name,
                "level": level,
                "name": f"{level}-{ttype}-{alias_name}",
                "global_path": f"{level}-{ttype}-{alias_name}"
            }
            
            return {
                    "type": "subquery",
                    "alias": alias_name
                }
            
        elif ttype == 'cte': # Здесь проблема с link на cte 
            alias_name = ast.alias
            name = self.lower_case(ast.name)
            semantic.tables[alias_name] = {
                "type": ttype,
                "alias": alias_name,
                "level": level, 
                "name": f"main-{ttype}-{name}",
                "global_path": f"main-{ttype}-{name}"
            }
            
            return {
                    "type": "cte",
                    "name": self.lower_case(ast.name),
                    "alias": alias_name
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
            
            return {
                    "type": "table",
                    "name": ".".join(
                        x for x in [
                            ast.catalog,
                            self.lower_case(ast.db),
                            self.lower_case(ast.name)
                        ]
                        if x
                    ),
                    "alias": alias_name
                }
        
        return {
                "type": type(ast).__name__
            }

    
    def extract_from(self, ast, semantic, semantic_master, level):
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

        self.extract_objects(ast_from, semantic, semantic_master, ttype, level)
    
    def is_join_key(self, node):
        return (
            isinstance(node, exp.EQ)
            and isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Column)
            and node.left.table != node.right.table
        )

    def extract_operand(self, node, semantic, level):

        if isinstance(node, exp.Column):
            if semantic.tables[node.table]:
                i = semantic.tables[node.table]
                return {
                    "table": i["name"],
                    "column": node.name
                }

        if isinstance(node, exp.Literal):
            return {
                "literal": node.this
            }

        return {
            "expression": node.sql()
        }
    
    
    def extract_conditions(self, node, semantic, level):
        """
        Возвращает список всех элементарных условий из ON.
        """

        if node is None:
            return []

        # Разбираем AND
        if isinstance(node, exp.And):
            return (
                self.extract_conditions(node.left, semantic, level)
                + self.extract_conditions(node.right, semantic, level)
            )

        # Разбираем OR
        if isinstance(node, exp.Or):
            return [{
                "operator": "OR",
                "conditions": (
                    self.extract_conditions(node.left, semantic, level)
                    + self.extract_conditions(node.right, semantic, level)
                )
            }]

        # Простое сравнение
        if isinstance(node, (
            exp.EQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.NEQ,
        )):
            
            return [{
                "operator": node.key,
                "left": self.extract_operand(node.left, semantic, level),
                "right": self.extract_operand(node.right, semantic, level),
                "is_join_key": self.is_join_key(node)
            }]

        return [{
            "expression": node.sql()
        }]
        
    def resolve_expression(self, node, semantic, level):
    
        columns = []

        for col in node.find_all(exp.Column): 

            if semantic.tables[col.table]:
                i = semantic.tables[col.table]
                columns.append({
                    "level": level,
                    "table": i["name"],
                    "column": col.name
                })

        return columns
    
    def extract_columns(self, ast, semantic, level):
    
        select = ast.find(exp.Select)

        if not select:
            return

        for expression in select.expressions:

            output_name = expression.name
            # alias = expression.alias

            semantic.column_lineage[f"{output_name}"] = self.resolve_expression(expression, semantic, level)
    
        
    def extract_joins(self, ast, semantic, semantic_master, level):
        select = ast.find(exp.Select)
        joins = select.args.get("joins", [])

        for join in joins:

            ttype = self.get_type_of_table(join.this)
            self.extract_objects(join.this, semantic, semantic_master, ttype, level)
            
            conditions = self.extract_conditions(join.args.get("on"), semantic, level)
            
            
            join_keys = [
                {k: v for k, v in c.items() if k != "is_join_key"}
                for c in conditions
                if c.get("is_join_key")
            ]

            join_type = (
                join.args.get("side")
                or join.args.get("kind")
                or "INNER"
            )

            join_type = self.lower_case(join_type)
        
            if join_keys:
                if join_type == "left":
                    join_info = {
                        "type": "left",
                        "table": self.get_join_source(join.this, semantic, semantic_master, level),
                        "join_keys": join_keys
                    }

                elif join_type == "right":
                    join_info = {
                        "type": "right",
                        "table": self.get_join_source(join.this, semantic, semantic_master, level),
                        "join_keys": join_keys
                    }

                elif join_type == "inner":
                    join_info = {
                        "type": "inner",
                        "table": self.get_join_source(join.this, semantic, semantic_master, level),
                        "join_keys": join_keys
                    }
                

            semantic.joins.append(join_info)
    
    def extract_subquery(self, ast, semantic, level):
        subqueries = list(find_all_in_scope(ast, exp.Subquery))
            
        if not subqueries:
            # print("Subquery нет")
            return

        for subquery in subqueries:
            
            key = f"{level}-subquery-{subquery.alias}"
            semantic.objects[key] = Objects(alias_name=subquery.alias, obj_type='subquery', parent=level)

            selects = list(self.extract_union_selects(subquery.this))

            for i in range(len(selects)):
                name = f'main_{i+1}'
                query = selects[i]
                subquery_level = f"{key}:{name}"

                semantic.objects[key].select_blocks.append(SelectBlock(block_name=name))
            
                self.extract_from(query, semantic.objects[key].select_blocks[i], semantic, subquery_level)

                self.extract_joins(query, semantic.objects[key].select_blocks[i], semantic, subquery_level)
                
                self.extract_columns(
                    query,
                    semantic.objects[key].select_blocks[i],
                    subquery_level
                )
                
    
    
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
                
                self.extract_from(query, semantic.objects[key].select_blocks[i], semantic, cte_level)
                
                self.extract_joins(query, semantic.objects[key].select_blocks[i], semantic, cte_level)
                
                self.extract_columns(query, semantic.objects[key].select_blocks[i], cte_level)
                
                
            
        
        print("Done")
    