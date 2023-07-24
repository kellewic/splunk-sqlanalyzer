import os, re, sys, traceback

file_realpath = os.path.realpath(__file__)
script_dir = os.path.dirname(file_realpath)

sys.path.insert(0, os.path.join(script_dir, "..", "lib"))
from splunklib.searchcommands import dispatch, StreamingCommand, Configuration, Option
from sqlglot import parse_one, exp

default_field_prefix = 'sqlacc_'
TABLE_SUBQUERY_NAME = "<Subquery>"
DML_SELECT = "SELECT"


@Configuration()
class SqlAnalyzerCommand(StreamingCommand):
    dialect = Option(
        doc='''
        **Syntax:** **dialect=***<sql dialect>*
        **Description:** SQL dialect to use in analysis''',
        require=True)

    field_prefix = Option(
            doc='''
            **Syntax:** **field_prefix=***<field prefix>*
            **Description:** Prefix for returned fields''',
            require=False)


    def parse_sql(self, sql_query):
        results = {
            self.error_key: None,
            self.tables_key: [],
            self.tables_count_key: 0,
            self.databases_key: [],
            self.databases_count_key: 0,
            self.subqueries_key: [],
            self.subqueries_count_key: 0,
            self.projections_key: [],
            self.projections_count_key: 0,
            self.fields_key: [],
            self.fields_count_key: 0,
            self.functions_key: [],
            self.functions_count_key: 0,
            self.command_key: None,
            self.ddl_key: [],
            self.ddl_count_key: 0,
            self.dml_key: [],
            self.dml_count_key: 0,
        }

        try:
            all_selects = []
            cur_select = []
            need_next_select = False

            query_ast = parse_one(sql_query, self.dialect)
            query_has_select = bool(len([n for n in query_ast.find_all(exp.Select)]))

            ## walk query AST looking for everything up to the Where while
            ## splitting up all Select object subqueries
            for item, parent, key in query_ast.walk():
                if isinstance(item, exp.Select):
                    need_next_select = False

                    ## if there's a parent then this is a subquery
                    if parent is not None:
                        results[self.subqueries_key].append(str(item))

                    results[self.dml_key].append(DML_SELECT)

                ## stop at From since we don't care about selections
                elif isinstance(item, exp.From):
                    ## consume From
                    cur_select.append(item)
                    all_selects.append(cur_select)

                    ## prepare for further Select statements
                    cur_select = []
                    need_next_select = True

                else:
                    ## consume everything up to From as we only care about projections
                    if need_next_select is False:
                        cur_select.append(item)


            ## if the query has a Select but doesn't have a From object then
            ## the cur_select items never get added to the main list so add
            ## them
            if query_has_select and len(all_selects) == 0:
                all_selects.append(cur_select)


            ## now we have Select objects from the entire query, we need to process
            ## columns, functions, commands, databases, and tables per Select
            alter_field = lambda tlen, t, f: tlen == 1 and "{}.{}".format(t, f) or f

            for items in all_selects:
                tables = []
                tables_len = 0
                table_aliases = {}

                ## if we have a From object, process it for table info
                if isinstance(items[-1], exp.From):
                    ## if From is a Subquery, use a placeholder as the table name
                    sub = items[-1].find(exp.Subquery)
                    if sub is not None:
                        sub.replace(exp.Table(this=TABLE_SUBQUERY_NAME))

                    for table in items.pop().find_all(exp.Table):
                        ## check if database name is included in table reference
                        db = table.args.get("db")
                        if db is not None:
                            results[self.databases_key].append(db.name)

                        ## map table aliases to table names
                        if isinstance(table.parent, exp.Alias):
                            table_aliases[str(table.parent.args.get("alias"))] = str(table)

                        ## save db.table names
                        results[self.tables_key].append(str(table))
                        tables.append(str(table))

                    tables_len = len(tables)


                ## process projections
                fields = []
                for item in items:
                    ## make sure the projection's parent is a Select. Due to how
                    ## the AST is parsed, sometimes this can be Where conditions
                    if not isinstance(item.parent, exp.Select):
                        continue

                    results[self.projections_key].append(str(item))

                    ## see if the projection has a Subquery and if so replace
                    ## it with a Literal "?" to avoid processing it in the
                    ## context of this Select
                    sub = item.find(exp.Subquery)
                    if sub is not None:
                        sub.replace(exp.Literal(this="?", is_string=True))

                    ## plain Star; prefix with table if only one exists
                    if isinstance(item, exp.Star):
                        fields.append(alter_field(tables_len, tables[0], "*"))

                    else:
                        for field in item.find_all(exp.Column):
                            ## unquoted fields are case-insensitive so convert to lowercase
                            if field.args.get("this").args.get("quoted") is False:
                                field_name = field.name.lower()

                            else:
                                field_name = field.name

                            ## override field_name if it's Star since field.name is blank in this instance
                            if isinstance(field.args.get("this"), exp.Star):
                                field_name = "*"

                            ## prefix column name with table if we can determine a match
                            field_table = field.args.get("table")
                            if field_table is not None:
                                field_table = str(field_table)
                                field_table = table_aliases.get(field_table, field_table)
                                field_table = alter_field(1, field_table, field_name)

                            else:
                                field_table = alter_field(tables_len, tables[0], field_name)

                            fields.append(field_table)


                        ## save any functions encountered
                        for func in item.find_all(exp.Func):
                            if isinstance(func, exp.Anonymous):
                                ## anonymous functions look like functions but aren't coded into the Python lib like pg_backend_pid()
                                results[self.functions_key].append(re.sub('\s*\(.*?\)\s*$', '', str(func)))
                            else:
                                results[self.functions_key].append(func.sql_name())


                results[self.fields_key].extend(set(fields))


            ## no select statements found, check for other possibilities
            if len(all_selects) == 0:
                for item, parent, key in parse_one(sql_query, self.dialect).walk():
                    if isinstance(item, exp.Command):
                        results[self.command_key] = item.name


        except Exception as ex:
            results[self.error_key] = traceback.format_exc()


        ## remove Subquery tables from the table list
        results[self.tables_key] = [t for t in results[self.tables_key] if t != TABLE_SUBQUERY_NAME]

        ## de-dupe all list entries and record length of each list
        for k in list(results.keys()):
            v = results[k]

            if v is not None and isinstance(v, list):
                if len(v) == 0:
                    results[k] = None
                else:
                    results[k] = list(set(results[k]))
                    results["{}_count".format(k)] = len(results[k])

        return results


    def stream(self, records):
        field_prefix = self.field_prefix

        if field_prefix is None:
            field_prefix = default_field_prefix

        self.error_key = "{}error".format(field_prefix)
        self.tables_key = "{}tables".format(field_prefix)
        self.tables_count_key = "{}tables_count".format(field_prefix)
        self.databases_key = "{}databases".format(field_prefix)
        self.databases_count_key = "{}databases_count".format(field_prefix)
        self.subqueries_key = "{}subqueries".format(field_prefix)
        self.subqueries_count_key = "{}subqueries_count".format(field_prefix)
        self.projections_key = "{}projections".format(field_prefix)
        self.projections_count_key = "{}projections_count".format(field_prefix)
        self.fields_key = "{}fields".format(field_prefix)
        self.fields_count_key = "{}fields_count".format(field_prefix)
        self.functions_key = "{}functions".format(field_prefix)
        self.functions_count_key = "{}functions_count".format(field_prefix)
        self.command_key = "{}command".format(field_prefix)
        self.ddl_key = "{}ddl".format(field_prefix)
        self.ddl_count_key = "{}ddl_count".format(field_prefix)
        self.dml_key = "{}dml".format(field_prefix)
        self.dml_count_key = "{}dml_count".format(field_prefix)

        for record in records:
            sql_query = record[self.fieldnames[0]]
            results = self.parse_sql(sql_query)

            record.update(results)

            yield record


dispatch(SqlAnalyzerCommand, sys.argv, sys.stdin, sys.stdout, __name__)


