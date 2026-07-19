import sqlglot
from sqlglot import exp

from dataclasses import dataclass, field, asdict

@dataclass
class Objects:
    alias_name: str
    obj_type: str
    tables: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    column_lineage: dict = field(default_factory=dict)

@dataclass
class SemanticJSON:
    
    metadata: dict = field(default_factory=dict) # Думаю буду использовать Jira/Confluence
    
    objects: dict[str, Objects] = field(default_factory=dict)
    
    tables: list = field(default_factory=list)

    joins: list = field(default_factory=list)
    


class SemanticExtractor:
    
    def extract(self, sql: str):
        
        ast = sqlglot.parse_one(sql) # AST(Abstract  Syntax Tree) - это дерево которое полностью повторяет SQL сиентакс
                
        semantic = SemanticJSON()        
        
        self.extract_objects(ast, semantic)
        
        self.extract_tables(ast, semantic)
        self.extract_joins(ast, semantic)
        
        relationships = self.extract_unique_relationships(semantic)
        
        # for k in semantic.objects.keys():
        #     semantic.tables.append(semantic.objects[k].tables)
        
        return semantic
        
    
    def lower_case(self, t):
        return str(t).lower()
    
    def get_join_source(self, node):
                
        # CTE
        if isinstance(node, exp.Table) and node.db == '':

            return {
                "type": "cte",
                "name": self.lower_case(node.name),
                "alias": node.alias_or_name
            }

        # Обычная таблица
        if isinstance(node, exp.Table):

            return {
                "type": "table",
                "name": ".".join(
                    x for x in [
                        self.lower_case(node.catalog),
                        self.lower_case(node.db),
                        self.lower_case(node.name)
                    ]
                    if x
                ),
                "alias": node.alias_or_name
            }

        # Подзапрос
        if isinstance(node, exp.Subquery):

            return {
                "type": "subquery",
                "alias": node.alias_or_name
            }

        return {
            "type": type(node).__name__
        }
        
    # Вывести таблицы
    def extract_tables(self, ast, semantic):
                
        for table in ast.find_all(exp.Table):

            semantic.tables.append({
                "table": self.lower_case(table.name),
                "alias": table.alias_or_name,
                table.alias_or_name: ".".join(
                    x for x in [(self.lower_case(table.catalog) or None), self.lower_case(table.db), self.lower_case(table.name)] if x
                )
            })
    
    # Вывести join-ы        
    def is_join_key(self, node):
        return (
            isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Column)
        )

    def extract_operand(self, node, semantic):

        if isinstance(node, exp.Column):
            
            for i in semantic.tables:
                for j in i.keys():
                    if j == node.table:
                        return {
                            "table": i[node.table],
                            "column": node.name
                        }
                    else:
                        continue

        if isinstance(node, exp.Literal):
            return {
                "literal": node.this
            }

        return {
            "expression": node.sql()
        }
    
    
    def extract_conditions(self, node, semantic):
        """
        Возвращает список всех элементарных условий из ON.
        """

        if node is None:
            return []

        # Разбираем AND
        if isinstance(node, exp.And):
            return (
                self.extract_conditions(node.left, semantic)
                + self.extract_conditions(node.right, semantic)
            )

        # Разбираем OR
        if isinstance(node, exp.Or):
            return [{
                "operator": "OR",
                "conditions": (
                    self.extract_conditions(node.left, semantic)
                    + self.extract_conditions(node.right, semantic)
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
                "left": self.extract_operand(node.left, semantic),
                "right": self.extract_operand(node.right, semantic),
                "is_join_key": self.is_join_key(node)
            }]

        return [{
            "expression": node.sql()
        }]
    
    
    def extract_joins(self, ast, semantic):
        
        for join in ast.find_all(exp.Join):    
            
            # subq_fl = False
            
            conditions = self.extract_conditions(join.args.get("on"), semantic)
            
            # if isinstance(join.this, exp.Subquery):
            #     subq_fl = True
            #     subq_name = f"subquery:{join.this.alias}"
            #     subq_columns = semantic.objects[subq_name].column_lineage
                
            #     print(subq_columns)
            
            
            join_keys = [
                {k: v for k, v in c.items() if k != "is_join_key"}
                for c in conditions
                if c.get("is_join_key")
            ]

        
            if join_keys:
                join_info = {
                    "type": self.lower_case(join.args.get("side", "INNER")),
                    "table": self.get_join_source(
                        join.this
                    ),
                    "join_keys": join_keys
                }

            semantic.joins.append(join_info)
            
    def resolve_expression(self, node, semantic):

        columns = []

        for col in node.find_all(exp.Column):
            for i in semantic.tables:
                for j in i.keys():
                    if j == col.table:
                        columns.append({
                            "table": i[col.table],
                            "column": col.name
                        })

        return columns
    
            
    def extract_columns(self, ast, semantic, alias):

        select = ast.find(exp.Select)

        if not select:
            return

        for expression in select.expressions:

            output_name = expression.alias_or_name

            semantic.column_lineage[f"{alias}.{output_name}"] = self.resolve_expression(expression, semantic)
            
    
    
    def extract_objects(self, ast, semantic):
        
        for cte in ast.find_all(exp.CTE):
            key = f"cte:{cte.alias}"
            
            semantic.objects[key] = Objects(alias_name=cte.alias, obj_type='cte')
            
            self.extract_tables(
                cte.this,
                semantic.objects[key]
            )
            
            self.extract_joins(
                cte.this,
                semantic.objects[key]
            )
            
            self.extract_columns(
                cte.this,
                semantic.objects[key],
                cte.alias
            )
        
        for subquery in ast.find_all(exp.Subquery):
            key = f"subquery:{subquery.alias}"
            
            semantic.objects[key] = Objects(alias_name=subquery.alias, obj_type='subquery')
            
            self.extract_tables(
                subquery.this,
                semantic.objects[key]
            )
            
            self.extract_joins(
                subquery.this,
                semantic.objects[key]
            )
            
            self.extract_columns(
                subquery.this,
                semantic.objects[key],
                subquery.alias
            )
    
    
    def resolve_column_lineage(
        self,
        operand,
        semantic,
        current_object=None
    ):

        # Физическая таблица
        if "table" in operand:

            return [
                {
                    "table": operand["table"],
                    "column": operand["column"]
                }
            ]

        if "expression" not in operand:
            return []

        expression = operand["expression"]

        if "." not in expression:
            return []

        alias, column = expression.split(
            ".",
            1
        )

        alias = self.lower_case(alias)

        # ----------------------------
        # SUBQUERY
        # ----------------------------

        subquery_key = f"subquery:{alias}"

        if subquery_key in semantic.objects:

            obj = semantic.objects[
                subquery_key
            ]

            return obj.column_lineage.get(
                expression,
                []
            )

        # ----------------------------
        # CTE
        # ----------------------------

        cte_key = f"cte:{alias}"

        if cte_key in semantic.objects:

            obj = semantic.objects[
                cte_key
            ]

            return obj.column_lineage.get(
                expression,
                []
            )

        return []
    
    def normalize_relationship(self, relationship):
        left = (
            relationship["left_table"],
            relationship["left_column"]
        )

        right = (
            relationship["right_table"],
            relationship["right_column"]
        )

        # Чтобы A = B и B = A считались одной связью
        if left > right:

            left, right = right, left

        return {
            "left_table": left[0],
            "left_column": left[1],

            "right_table": right[0],
            "right_column": right[1]
        }
    
    
    def extract_unique_relationships(self, semantic):

        unique_relationships = set()

        relationships = []

        for join in semantic.joins:

            for condition in join["join_keys"]:

                left = condition["left"]
                right = condition["right"]

                left_columns = self.resolve_column_lineage(
                    left,
                    semantic
                )

                right_columns = self.resolve_column_lineage(
                    right,
                    semantic
                )

                for left_col in left_columns:

                    for right_col in right_columns:

                        left_key = (
                            left_col["table"],
                            left_col["column"]
                        )

                        right_key = (
                            right_col["table"],
                            right_col["column"]
                        )

                        # Делаем связь независимой от направления
                        relationship_key = tuple(
                            sorted([
                                left_key,
                                right_key
                            ])
                        )

                        if relationship_key in unique_relationships:
                            continue

                        unique_relationships.add(
                            relationship_key
                        )

                        relationships.append({

                            "left_table": relationship_key[0][0],
                            "left_column": relationship_key[0][1],

                            "right_table": relationship_key[1][0],
                            "right_column": relationship_key[1][1],

                            "operator": condition["operator"],

                            "join_type": join["type"]
                        })

        return relationships
        