import sqlglot
from sqlglot import exp
import json
import os

from dataclasses import dataclass, field, asdict

JSON_PATH = 'output\\data.json'

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
    
    def extract(self, sql: str):
        
        ast = sqlglot.parse_one(sql) # AST(Abstract  Syntax Tree) - это дерево которое полностью повторяет SQL сиентакс
                
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
            self.extract_from(query, semantic.mains[name], level)
            self.extract_joins(query, semantic.mains[name], level)

        
        relationships_print = self.extract_unique_relationships_print(semantic)
        relationships_json = self.extract_unique_relationships_json(semantic)
        
        self.merge_relationships(relationships_json)

        return relationships_print, relationships_json
        
    def merge_relationships(self, new_relationships):

        if not os.path.exists(JSON_PATH):
            with open(JSON_PATH, "w") as f:
                json.dump({}, f, indent=4)
        
        try:
            with open(JSON_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            data = {}

        existing = set(data.keys())

        for key, rel in new_relationships.items():

            if key not in existing:
                data[key] = rel
                existing.add(key)

        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    
    
    def lower_case(self, t):
        return str(t).lower()

    def extract_union_selects(self, node):
        if isinstance(node, exp.Union):
            yield from self.extract_union_selects(node.this)
            yield from self.extract_union_selects(node.expression)
        else:
            yield node
    
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
    def extract_tables(self, ast, semantic, ttype, level):

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
            for table in ast.find_all(exp.Table):
                alias_name = table.alias
                semantic.tables[alias_name] = {
                    "type": ttype,
                    "alias": table.alias,
                    "level": level,
                    "name": ".".join(
                        x for x in [table.catalog, self.lower_case(table.db), self.lower_case(table.name)] if x
                    ),
                    "global_path": f"{level}"
                }

    def get_type_of_table(self, ast, semantic):
        ttype = ''

        if isinstance(ast, exp.Table) and ast.db == '':
            ttype = 'cte'
        elif isinstance(ast, exp.Table) and ast.db != '':
            ttype = 'table'
        elif isinstance(ast, exp.Subquery):
            ttype = 'subquery'

        return ttype

    def extract_from(self, ast, semantic, level):
        ast_from = ast.find(exp.From).this
        
        ttype = self.get_type_of_table(ast_from, semantic)

        self.extract_tables(ast_from, semantic, ttype, level)

        

    
    # Вывести join-ы        
    def is_join_key(self, node):
        return (
            isinstance(node.left, exp.Column)
            and isinstance(node.right, exp.Column)
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
    
    
    def extract_joins(self, ast, semantic, level):
        select = ast.find(exp.Select)
        joins = select.args.get("joins", [])

        for join in joins:

            ttype = self.get_type_of_table(join.this, semantic)
            self.extract_tables(join.this, semantic, ttype, level)
            
            conditions = self.extract_conditions(join.args.get("on"), semantic, level)
            
            
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
    
            
    def extract_columns(self, ast, semantic, alias, level):

        select = ast.find(exp.Select)

        if not select:
            return

        for expression in select.expressions:

            output_name = expression.alias_or_name

            semantic.column_lineage[f"{alias}.{output_name}"] = self.resolve_expression(expression, semantic, level)


    def extract_subquery(self, ast, semantic, level):
        subqueries = list(ast.find_all(exp.Subquery, bfs=False))
            
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
            
                self.extract_from(query, semantic.objects[key].select_blocks[i], subquery_level)

                self.extract_joins(
                    query,
                    semantic.objects[key].select_blocks[i],
                    subquery_level
                )
                
                self.extract_columns(
                    query,
                    semantic.objects[key].select_blocks[i],
                    query.alias,
                    subquery_level
                )
                
                self.extract_subquery(
                    query,
                    semantic,
                    subquery_level,
                )

    def extract_cte(self, ast, semantic, level=None):
        ctes = list(ast.find_all(exp.CTE, bfs=False))
    
        if not ctes:
            print("CTE нет")
            return

        for cte in ctes:

            key = f"main-cte-{cte.alias}"
            semantic.objects[key] = Objects(alias_name=cte.alias, obj_type='cte', parent='main')

            selects = list(self.extract_union_selects(cte.this))
                    
            for i in range(len(selects)):
                name = f'main_{i+1}'
                query = selects[i]
                cte_level = f"{key}:{name}"

                semantic.objects[key].select_blocks.append(SelectBlock(block_name=name))

                self.extract_from(query, semantic.objects[key].select_blocks[i], cte_level)

                self.extract_joins(
                                query,
                                semantic.objects[key].select_blocks[i],
                                cte_level
                            )

                self.extract_columns(
                    query,
                    semantic.objects[key].select_blocks[i],
                    query.alias,
                    cte_level
                )
                
                self.extract_subquery(
                    query,
                    semantic,
                    cte_level
                )
                
                self.extract_cte(
                    query,
                    semantic,
                    cte_level
                )

        
    
    # ==============================================================================================================================
    
    # =====================================================================
    # RELATIONSHIPS
    # =====================================================================

    def get_source_type(
        self,
        source_name,
        semantic
    ):
        """
        Определяет тип источника.

        Если source_name найден в semantic.objects,
        значит это CTE или Subquery.

        Если не найден — это физическая таблица.

        Например:

            main-cte-main
                -> cte

            main-cte-table_1
                -> cte

            main_1-subquery-m2
                -> subquery

            cbs.main
                -> table
        """

        obj = semantic.objects.get(
            source_name
        )

        if obj is not None:
            return obj.obj_type

        return "table"


    def resolve_column_sources(
        self,
        source_name,
        column_name,
        semantic,
        visited=None
    ):
        """
        Рекурсивно раскрывает:

            source.column

        до физических таблиц.

        Пример:

            main-cte-table_1.id
                    |
                    v
            cbs.c_user.id

        Результат:

            [
                {
                    "table": "cbs.c_user",
                    "column": "id",
                    "type": "table"
                }
            ]


        Пример с UNION:

            main-cte-main.id
                    |
                    +-- cbs.main.id
                    |
                    +-- cbs.other.id

        Результат:

            [
                {
                    "table": "cbs.main",
                    "column": "id",
                    "type": "table"
                },
                {
                    "table": "cbs.other",
                    "column": "id",
                    "type": "table"
                }
            ]
        """

        if visited is None:
            visited = set()


        # ================================================================
        # Защита от циклических зависимостей
        # ================================================================

        current_key = (
            source_name,
            column_name
        )

        if current_key in visited:
            return []


        # Создаем новый набор для текущей ветки.
        #
        # Это важно для UNION.
        #
        # Например:
        #
        # main.id
        #   |
        #   +-- cbs.main.id
        #
        #   +-- cbs.other.id
        #
        # Каждая ветка должна иметь собственный visited.
        #

        current_visited = visited | {
            current_key
        }


        # ================================================================
        # Определяем тип источника
        # ================================================================

        source_type = self.get_source_type(
            source_name,
            semantic
        )


        # ================================================================
        # ФИЗИЧЕСКАЯ ТАБЛИЦА
        # ================================================================

        if source_type == "table":

            return [
                {
                    "table": source_name,
                    "column": column_name,
                    "type": "table"
                }
            ]


        # ================================================================
        # CTE / SUBQUERY
        # ================================================================

        obj = semantic.objects.get(
            source_name
        )


        # Если объект не найден,
        # считаем его физической таблицей.
        #
        # Это дополнительная защита.
        #

        if obj is None:

            return [
                {
                    "table": source_name,
                    "column": column_name,
                    "type": "table"
                }
            ]


        result = []


        # ================================================================
        # Проходим по всем SELECT-блокам объекта
        #
        # Это особенно важно для UNION.
        #
        # Например:
        #
        # main:
        #
        # SELECT id FROM cbs.main
        #
        # UNION ALL
        #
        # SELECT id FROM cbs.other
        #
        # У объекта будет несколько select_blocks.
        #
        # Нужно обработать каждый.
        # ================================================================

        for block in obj.select_blocks:


            # ------------------------------------------------------------
            # В твоем semantic column_lineage хранится примерно так:
            #
            # {
            #     ".id": [
            #         {
            #             "table": "cbs.main",
            #             "column": "id"
            #         }
            #     ]
            # }
            #
            # Поэтому сначала ищем точное совпадение.
            # ------------------------------------------------------------

            lineage_entries = block.column_lineage.get(
                f".{column_name}"
            )


            # ------------------------------------------------------------
            # Если точного совпадения нет,
            # ищем любой ключ, заканчивающийся на .column_name.
            #
            # Например:
            #
            # m.id
            # user.id
            # .id
            #
            # ------------------------------------------------------------

            if lineage_entries is None:

                for key, value in block.column_lineage.items():

                    if key.endswith(
                        f".{column_name}"
                    ):

                        lineage_entries = value

                        break


            # Нет lineage для этой колонки
            if not lineage_entries:
                continue


            # ============================================================
            # Рекурсивно раскрываем каждый источник
            # ============================================================

            for entry in lineage_entries:


                nested_sources = self.resolve_column_sources(
                    source_name=entry["table"],
                    column_name=entry["column"],
                    semantic=semantic,
                    visited=current_visited
                )


                result.extend(
                    nested_sources
                )


        # ================================================================
        # Удаляем дубликаты
        # ================================================================

        return self.deduplicate_sources(
            result
        )


    def deduplicate_sources(
        self,
        sources
    ):
        """
        Удаляет дубликаты физических источников.

        Например:

            [
                {
                    "table": "cbs.main",
                    "column": "id"
                },
                {
                    "table": "cbs.main",
                    "column": "id"
                }
            ]

        превращается в:

            [
                {
                    "table": "cbs.main",
                    "column": "id"
                }
            ]
        """

        result = []

        visited = set()


        for source in sources:


            key = (
                source["table"],
                source["column"]
            )


            if key in visited:
                continue


            visited.add(
                key
            )


            result.append(
                source
            )


        return result


    # =====================================================================
    # NORMALIZE RELATIONSHIP
    # =====================================================================

    def normalize_relationship(
        self,
        relationship
    ):
        """
        Делает связь независимой от направления.

        Например:

            A.id = B.id

        и:

            B.id = A.id

        будут представлены одинаково.
        """


        left = (
            relationship["left_table"],
            relationship["left_column"]
        )


        right = (
            relationship["right_table"],
            relationship["right_column"]
        )


        # Сортируем стороны
        if left > right:

            left, right = right, left


        return {

            "left_table": left[0],

            "left_column": left[1],

            "right_table": right[0],

            "right_column": right[1],

            "operator": relationship.get(
                "operator"
            ),

            "join_type": relationship.get(
                "join_type"
            )

        }

    def process_join(
        self,
        join,
        semantic
    ):
        """
        Обрабатывает один JOIN.

        Например:

            main.id = t.id

        Сначала:

            main.id
                |
                v
            cbs.main.id

            t.id
                |
                v
            cbs.t_dea.id

        Затем возвращает:

            {
                "left_table": "cbs.main",
                "left_column": "id",

                "right_table": "cbs.t_dea",
                "right_column": "id",

                "operator": "eq",
                "join_type": "inner"
            }
        """

        relationships = []


        # ================================================================
        # Тип JOIN
        # ================================================================

        join_type = join.get(
            "type"
        )


        # ================================================================
        # Все условия JOIN
        # ================================================================

        join_keys = join.get(
            "join_keys",
            []
        )


        # ================================================================
        # Обрабатываем каждое условие
        #
        # Например:
        #
        # m.id = t.id
        #
        # m.dep_id = t.dep_id
        #
        # ================================================================

        for condition in join_keys:


            left = condition["left"]

            right = condition["right"]


            # ============================================================
            # Раскрываем левую колонку
            # ============================================================

            left_sources = self.resolve_column_sources(

                source_name=left["table"],

                column_name=left["column"],

                semantic=semantic

            )


            # ============================================================
            # Раскрываем правую колонку
            # ============================================================

            right_sources = self.resolve_column_sources(

                source_name=right["table"],

                column_name=right["column"],

                semantic=semantic

            )


            # ============================================================
            # Создаем комбинации физических таблиц
            #
            # Например:
            #
            # LEFT:
            #
            #   cbs.main.id
            #   cbs.other.id
            #
            # RIGHT:
            #
            #   cbs.t_dea.id
            #
            #
            # Результат:
            #
            #   cbs.main.id = cbs.t_dea.id
            #
            #   cbs.other.id = cbs.t_dea.id
            #
            # ============================================================

            for left_source in left_sources:

                for right_source in right_sources:


                    # ----------------------------------------------------
                    # Пропускаем связь колонки самой с собой
                    # ----------------------------------------------------

                    if (

                        left_source["table"]
                        == right_source["table"]

                        and

                        left_source["column"]
                        == right_source["column"]

                    ):

                        continue


                    # ----------------------------------------------------
                    # Создаем relationship
                    # ----------------------------------------------------

                    relationship = {

                        "left_table":
                            left_source["table"],

                        "left_column":
                            left_source["column"],

                        "right_table":
                            right_source["table"],

                        "right_column":
                            right_source["column"],

                        "operator":
                            condition.get(
                                "operator"
                            ),

                        "join_type":
                            join_type

                    }


                    # ----------------------------------------------------
                    # Нормализуем направление
                    # ----------------------------------------------------

                    relationship = self.normalize_relationship(

                        relationship

                    )


                    relationships.append(

                        relationship

                    )


        return relationships
    
    
    # =====================================================================
    # EXTRACT UNIQUE RELATIONSHIPS
    # =====================================================================
    
    def get_relationship_key(
        self,
        relationship
    ):
        """
        Формирует уникальный ключ relationship.

        Пример:

            cbs.c_user.id
            =
            cbs.e_reqexc.tus_id

        Ключ:

            cbs.c_user.id|eq|cbs.e_reqexc.tus_id
        """

        left = (
            f"{relationship['left_table']}."
            f"{relationship['left_column']}"
        )

        right = (
            f"{relationship['right_table']}."
            f"{relationship['right_column']}"
        )

        operator = relationship.get(
            "operator",
            ""
        )

        return (
            f"{left}|"
            f"{operator}|"
            f"{right}"
        )
        
    def extract_unique_relationships_json(
        self,
        semantic
    ):

        relationships = {}


        # ================================================================
        # MAIN
        # ================================================================

        for main in semantic.mains.values():

            for join in main.joins:

                join_relationships = self.process_join(
                    join,
                    semantic
                )


                for relationship in join_relationships:

                    relationship_key = (
                        self.get_relationship_key(
                            relationship
                        )
                    )


                    if relationship_key not in relationships:

                        relationships[
                            relationship_key
                        ] = relationship


        # ================================================================
        # CTE / SUBQUERY
        # ================================================================

        for obj in semantic.objects.values():

            for block in obj.select_blocks:

                for join in block.joins:

                    join_relationships = self.process_join(
                        join,
                        semantic
                    )


                    for relationship in join_relationships:

                        relationship_key = (
                            self.get_relationship_key(
                                relationship
                            )
                        )


                        if relationship_key not in relationships:

                            relationships[
                                relationship_key
                            ] = relationship


        return relationships

    def extract_unique_relationships_print(
        self,
        semantic
    ):
        """
        Извлекает уникальные relationships
        между физическими таблицами.
        """

        relationships = []

        unique_relationships = set()


        # ================================================================
        # MAIN QUERIES
        # ================================================================

        for main in semantic.mains.values():

            for join in main.joins:


                join_relationships = self.process_join(

                    join,

                    semantic

                )


                for relationship in join_relationships:


                    relationship_key = (

                        relationship["left_table"],

                        relationship["left_column"],

                        relationship["right_table"],

                        relationship["right_column"],

                        relationship["operator"]

                    )


                    if relationship_key in unique_relationships:

                        continue


                    unique_relationships.add(

                        relationship_key

                    )


                    relationships.append(

                        relationship

                    )


        # ================================================================
        # CTE / SUBQUERY
        # ================================================================

        for obj in semantic.objects.values():

            for block in obj.select_blocks:

                for join in block.joins:


                    join_relationships = self.process_join(

                        join,

                        semantic

                    )


                    for relationship in join_relationships:


                        relationship_key = (

                            relationship["left_table"],

                            relationship["left_column"],

                            relationship["right_table"],

                            relationship["right_column"],

                            relationship["operator"]

                        )


                        if relationship_key in unique_relationships:

                            continue


                        unique_relationships.add(

                            relationship_key

                        )


                        relationships.append(

                            relationship

                        )


        return relationships
