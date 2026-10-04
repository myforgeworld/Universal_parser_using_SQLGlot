import sqlglot
from sqlglot import exp
from dataclasses import dataclass

# A class for storing information about syntactic errors that may prevent scripts from being parsed correctly
@dataclass
class ValidationErrorList:
    code: str
    message: str
    line: int | None=None
    column: int | None=None
    
    def __str__(self):
        location = ""
        
        if self.line is not None:
            location = f" [{self.line}:{self.column}]"
        
        return f"{self.code}{location}: {self.message}"


# For script parsing to work, you need to understand which column belongs to which table.
# The first thing to check is whether all tables, subqueries, and CTEs have aliases. 

class ValidationCheker():
    
    def check(self, sql: str):
        errors = []
        # AST(Abstract  Syntax Tree) - это дерево которое полностью повторяет SQL сиентакс
        ast = sqlglot.parse_one(sql)

        # first "ctes"
        other_query = ast.copy()
        ast_other = other_query.args.get("with_")

        if ast_other:
            self.extract_cte(ast_other, errors)

        # everything except “ctes”
        main_query = ast.copy()
        main_query.args.pop("with_", None) # Удаляем все кроме основного запроса

        selects = list(self.extract_union_selects(main_query))
        
        for i in range(len(selects)):
            query = selects[i]
            query.set("where", None) # We don't need "where"
            
            self.extract_from(query, errors)
            self.extract_joins(query, errors)
            self.extract_columns(query, errors)

        return errors
    
    def extract_union_selects(self, node):
        if isinstance(node, exp.Union):
            yield from self.extract_union_selects(node.this)
            yield from self.extract_union_selects(node.expression)
        else:
            yield node
    
    def table_alias_error(self, ast, errors):
        code = "T001"
        errors.append(
            ValidationErrorList(
                code=code,
                message=(
                    f'Table "{ast.sql()}" must have an alias'
                ),
                line=ast.meta.get("line"),
                column=ast.meta.get("col"),
            )
        )
        
    def validate_operand(self, node, errors):
        if not isinstance(node, exp.Column):
            return

        if not node.table:
            errors.append(
                ValidationErrorList(
                    code="C001",
                    message=(
                        f'Column "{node.sql()}" '
                        f'must have a table alias'
                    ),
                    line=node.meta.get("line"),
                    column=node.meta.get("col"),
                )
            )
    
    def no_star_rule(self, node, errors):
        """
        S001 — запрещает любой wildcard в SELECT.

        Запрещено:
            SELECT *
            SELECT p.*
            SELECT schema.p.*
        """

        star = None

        if isinstance(node, exp.Star):
            star = node

        elif isinstance(node, exp.Column):
            if isinstance(node.this, exp.Star):
                star = node.this

        if star is None:
            return

        errors.append(
            ValidationErrorList(
                code="S001",
                message=(
                    f'Wildcard "*" is not allowed in SELECT: "{node.sql()}"'
                ),
                line=node.meta.get("line"),
                column=node.meta.get("col"),
            )
        )
    
    def column_table_alias_rule(self, node, errors):
        """
        C002 — каждая колонка в SELECT должна иметь
        явный alias таблицы.

        Разрешено:
            SELECT p.id
            SELECT s.symbol

        Запрещено:
            SELECT id
            SELECT symbol
        """

        if not isinstance(node, exp.Column):
            return

        # Если у колонки нет table alias
        if not node.table:
            errors.append(
                ValidationErrorList(
                    code="C002",
                    message=(
                        f'Column "{node.sql()}" '
                        f'must have a table alias'
                    ),
                    line=node.meta.get("line"),
                    column=node.meta.get("col"),
                )
            )
    
    def extract_columns(self, ast, errors):
        select = ast.find(exp.Select)

        if not select:
            return

        for expression in select.expressions:
            # S001 — запрещаем *
            self.no_star_rule(
                expression,
                errors
            )

            # C002 — колонка должна иметь alias таблицы
            self.column_table_alias_rule(
                expression,
                errors
            )
            
    
    def get_type_of_table(self, ast):
        ttype = ''

        if isinstance(ast, exp.Table) and ast.db == '':
            ttype = 'cte'
        elif isinstance(ast, exp.Table) and ast.db != '':
            ttype = 'table'
        elif isinstance(ast, exp.Subquery):
            ttype = 'subquery'

        return ttype
    
    def from_block_check(self, ast):
        ast_from = ast.find(exp.From)
        
        if ast_from is None:
            print(f"[WARNING] FROM не найден: {type(ast).__name__}")
            print(ast)
            return 0

        ast_from = ast_from.this

        if ast_from is None:
            print(f"[WARNING] FROM.this пустой: {type(ast).__name__}")
            print(ast)
            return 0
    
    def extract_subquery(self, ast, errors):
        subqueries = list(ast.find_all(exp.Subquery, bfs=False))
            
        if not subqueries:
            print("We don't have Subquery")
            return

        for subquery in subqueries:

            if self.from_block_check(subquery.this) == 0:
                continue

            selects = list(self.extract_union_selects(subquery.this))

            for i in range(len(selects)):
                query = selects[i]
                query.set("where", None)
            
                self.extract_from(query, errors)

                self.extract_joins( query, errors)
                
                self.extract_columns(
                    query,
                    errors
                )
                
    
    def extract_tables(self, ast, ttype, errors):
        if ttype == 'subquery':
            alias_name = ast.alias
            
            if not alias_name:
                self.table_alias_error(ast, errors)
                
            self.extract_subquery(ast, errors)
                
        elif ttype == 'cte':
            alias_name = ast.alias
            
            if not alias_name:
                self.table_alias_error(ast, errors)
            
        elif ttype == 'table':
            alias_name = ast.alias
                   
            if not alias_name:
                self.table_alias_error(ast, errors)
    
    
    def extract_from(self, ast, errors):
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

        self.extract_tables(ast_from, ttype, errors)
                
        
    def extract_conditions(self, node, errors):
        """
        Возвращает список всех элементарных условий из ON.
        """
        
        if node is None:
            return []

        if isinstance(node, bool):
            return [{
                "expression": str(node).upper(),
                "is_join_key": False
            }]


        # Убираем скобки
        if isinstance(node, exp.Paren):
            return self.extract_conditions(
                node.this, errors
            )

        # Разбираем AND
        if isinstance(node, exp.And):
            return (
                self.extract_conditions(node.left, errors)
                +
                self.extract_conditions(node.right, errors)
            )

        # Разбираем OR
        if isinstance(node, exp.Or):
            return (
                self.extract_conditions(node.left, errors)
                +
                self.extract_conditions(node.right, errors)
            )

        # Игнорируем IN
        if isinstance(node, exp.In):
            return None

        # Простое сравнение
        if isinstance(node, (
            exp.EQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.NEQ,
        )):
            self.validate_operand(node.left, errors)
            self.validate_operand(node.right, errors)
            
            return [{
                "expression": node.sql(),
                "is_join_key": False
            }]
        
        return []

    
    def extract_joins(self, ast, errors):
        select = ast.find(exp.Select)
        joins = select.args.get("joins", [])

        for join in joins:
            ttype = self.get_type_of_table(join.this)
            self.extract_tables(join.this, ttype, errors)
            
            on = join.args.get("on")
            
            self.extract_conditions(join.args.get("on"), errors)

    
    def extract_cte(self, ast, errors):    
        ctes = list(ast.find_all(exp.CTE, bfs=False))
        
        if not ctes:
            print("Script don't have 'CTE'")
            return

        for cte in ctes:

            selects = list(self.extract_union_selects(cte.this))
                    
            for i in range(len(selects)):
                query = selects[i]
                query.set("where", None)
                
                self.extract_cte(query, errors)

                self.extract_from(query, errors)

                self.extract_joins(query, errors)

                self.extract_columns(
                    query,
                    errors
                )
        
        
