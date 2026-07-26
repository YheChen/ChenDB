# Milestone 2 — SQL parser and AST explorer

**Status: complete.** Engine version 0.2.0.

## Goal

Turn SQL text into a tree, with every node knowing exactly which characters it
came from — then make that link visible, so clicking a node in the AST
highlights the SQL that produced it.

Nothing executes. That is Milestone 3, and the API says so: there is a
`POST /parse` and no `POST /query`.

---

## What was built

### Engine — `engine/parser/`

| Module | Responsibility |
|---|---|
| `tokens.py` | `Token`, `TokenType`, `Keyword`, `SourceSpan` |
| `lexer.py` | hand-written scanner: one pass, one character of lookahead |
| `ast.py` | frozen dataclass nodes; generic `children()` / `attributes()` |
| `parser.py` | recursive descent, one method per grammar rule |
| `analyze.py` | `analyze_sql()` — never raises, returns partial results |

Five new diagnostic events in the `parser` category: `TokenizedEvent`,
`TokenEvent`, `AstNodeCreatedEvent`, `ParsedEvent`, `ParseErrorEvent`.

### Grammar

```
script         := statement { ';' statement } [ ';' ]
statement      := create_table | insert | select

create_table   := CREATE TABLE [ IF NOT EXISTS ] ident
                  '(' column_def { ',' column_def } ')'
column_def     := ident type_name { constraint }
constraint     := NOT NULL | NULL | PRIMARY KEY

insert         := INSERT INTO ident [ '(' ident { ',' ident } ')' ]
                  VALUES row { ',' row }
row            := '(' expr { ',' expr } ')'

select         := SELECT select_list FROM ident [ WHERE expr ]
select_item    := '*' | expr [ [ AS ] ident ]

expr           := or_expr
or_expr        := and_expr { OR and_expr }
and_expr       := not_expr { AND not_expr }
not_expr       := NOT not_expr | comparison
comparison     := additive [ ( '=' | '<>' | '<' | '<=' | '>' | '>=' ) additive
                           | IS [ NOT ] NULL ]
additive       := multiplicative { ( '+' | '-' ) multiplicative }
multiplicative := unary { ( '*' | '/' | '%' ) unary }
unary          := ( '-' | '+' ) unary | primary
primary        := literal | column_ref | '(' expr ')'
column_ref     := ident [ '.' ident ]
```

Precedence is encoded in the *shape* of the rules, not a table: `or_expr` calls
`and_expr` calls `not_expr`, so the operator parsed at the shallowest level
binds loosest. `a OR b AND c` therefore parses as `a OR (b AND c)`
automatically. Left associativity comes from the `while` loops — `1 - 2 - 3`
becomes `(1 - 2) - 3`.

### API

```
POST /api/v1/databases/{db}/parse   { "sql": "..." }
```

Always returns 200. Invalid SQL is a *result*, not a failed request: the editor
needs the tokens that did scan plus the error position, and an HTTP error would
throw both away.

### Visualizer — the SQL workspace

Monaco editor with a ChenDB-specific SQL grammar, a token stream, an AST tree,
and two-way highlighting. Error and warning markers appear at the exact
character. The workspace tab only exists because `/health` now reports
`features.sql: true`.

---

## The demo

```
SELECT email, age * 2 AS doubled
FROM users
WHERE age >= 18 AND email IS NOT NULL;
```

The AST, with the source fragment each node covers on the right:

```
SelectStatement                     SELECT email, age * 2 AS doubled FROM …
└─ SelectItem                       email
   └─ ColumnRef       email         email
└─ SelectItem doubled alias=doubled age * 2 AS doubled
   └─ BinaryOp        *             age * 2
      └─ ColumnRef    age           age
      └─ Literal      2             2
└─ TableRef           users         users
└─ BinaryOp           AND           age >= 18 AND email IS NOT NULL
   └─ BinaryOp        >=            age >= 18
      └─ ColumnRef    age           age
      └─ Literal      18            18
   └─ IsNullTest      IS NOT NULL   email IS NOT NULL
      └─ ColumnRef    email         email
```

Click `BinaryOp AND` and the editor highlights
`age >= 18 AND email IS NOT NULL` — not the `AND`, the whole subtree's source.
Put the cursor inside `18` and the `Literal` node selects, because the tree is
searched for the *innermost* node containing that offset.

From the CLI:

```python
from engine.parser import parse, walk

sql = "SELECT name FROM users WHERE age >= 18"
for node in walk(parse(sql)[0]):
    print(f"{node.node_type:<18} {node.span!r:<20} {node.text_in(sql)!r}")
```

---

## Decisions worth naming

**`IS NULL` is its own node.** Not `BinaryOp(EQ, x, Literal(None))`, because
`x = NULL` is a genuinely different thing: in three-valued logic it is UNKNOWN
for every input, including NULL. Keeping them distinct in the AST makes
conflating them impossible downstream — one of the classic SQL bugs, designed
out rather than commented about.

**Every keyword is reserved from the start**, including ones the parser
rejects. Recognising `ORDER` lets the parser say "ORDER BY is not implemented
yet" instead of "unexpected identifier 'ORDER'", and it means a future milestone
cannot silently break a query that used the word as a column name.
`"order"` in double quotes is still a valid name.

**`UnsupportedSqlError` is separate from `ParseError`.** "You wrote this wrong"
and "ChenDB cannot do this yet" are different messages, and only the second
should mention a milestone. The UI shows the first as a red error and the second
as an amber warning.

**`analyze_sql()` never raises.** A half-typed query is the normal state of a
query being written; an editor that goes blank on every keystroke is useless. It
returns whatever scanned, whatever parsed, and the error.

**NULL has no type.** `Literal(value=None, data_type=None)`. Its type comes
from the column it is compared against, which needs the catalog — Milestone 4.

**Node ids are assigned bottom-up** as rules complete, so the
`AstNodeCreatedEvent` order literally shows recursive descent assembling the
tree: leaves first, root last.

---

## A bug worth recording

`InsertStatement.rows` was first written as `tuple[tuple[Expression, ...], ...]`.
The generic `Node.children()` flattens one level of tuple, so the inner tuples —
being tuples, not `Node`s — matched nothing, and **every inserted value was
invisible to the tree walk**. The API returned an `INSERT` AST with no literals
in it.

The fix was a `ValuesRow` node wrapping each row, which is better design anyway:
a `VALUES` group is a real syntactic construct with its own span, so the UI can
highlight `(1, 'Ada')` as a unit. It also restores a clean invariant — *every
node field is a scalar, a `Node`, or a flat tuple* — now enforced for every node
type by `test_no_node_hides_children_inside_a_nested_tuple`.

---

## Complexity

| Operation | Cost |
|---|---|
| Tokenize | O(n) characters, single pass, one char lookahead, no backtracking |
| Parse | O(n) tokens, one token lookahead, no backtracking |
| `walk(node)` | O(nodes) |
| `innermostNodeAt(offset)` | O(nodes) — fine for a statement; an interval tree would be needed for a very large script |
| Node span union | O(1) |

Recursion depth is bounded by expression nesting. `MAX_EXPRESSION_DEPTH = 100`
turns a hostile `((((…))))` into a clean `ParseError` instead of a Python
`RecursionError`.

Measured: a 2000-column `SELECT` (roughly 4000 tokens) tokenizes and parses in
well under a millisecond. Parsing is not the bottleneck in any query.

---

## How real systems differ

**PostgreSQL** generates its lexer with `flex` (`scan.l`) and its parser with
Bison (`gram.y`), an LALR grammar. That scales to a far larger dialect and
resolves ambiguity mechanically, but a shift-reduce conflict cannot explain
itself — "syntax error at or near" is the limit of what it can tell you.
PostgreSQL then *transforms* the raw parse tree into a `Query` (name resolution,
type coercion) before planning; that transform is what ChenDB's Milestone 4
binder will be.

**SQLite** hand-writes its tokenizer (`tokenize.c`) and generates its parser
with its own tool, Lemon. It parses straight into bytecode rather than keeping a
statement tree, though it does keep an `Expr` tree for expressions. ChenDB
follows SQLite on the lexer and keeps a full AST, because the whole point here
is to be able to *look* at the query before running it.

**Why recursive descent at all**: it is what essentially every hand-written
production parser uses — SQLite, Clang, TypeScript, Go — because the failure
point is a specific method, so the error can name what it expected.

---

## Tests

188 new Python tests (470 total), 11 new frontend tests (53 total).

| File | Covers |
|---|---|
| `tests/unit/test_lexer.py` | every token type, spans slicing back to lexemes, line/column, `''` escapes, comments, unterminated inputs, `123abc`, linearity |
| `tests/unit/test_parser.py` | precedence and associativity for every level, `IS NULL` vs `= NULL`, all type spellings, constraint validation, `INSERT` arity, node id uniqueness, parent spans containing children, unsupported-SQL messages, depth limit, `analyze_sql` partial results |
| `tests/integration/test_parse_api.py` | flat AST addressability, spans, labels, attributes, partial results, lex vs parse failure, parser events in the shared timeline, bottom-up event order, and that parsing reads no pages |
| `visualizer/src/features/sql/PipelinePanel.test.tsx` | tree reassembly from the flat list, selection, token view, empty states |

Two worth calling out:

**`test_every_token_span_slices_back_to_its_own_lexeme`** — for every token in a
multi-line input, `sql[token.start:token.end] == token.lexeme`. If that holds,
the editor's highlighting cannot be off by one.

**`test_a_parent_span_contains_every_child_span`** — an invariant of the whole
span design. If a child ever escaped its parent's range, selecting the parent
would highlight less than the subtree it owns.

---

## Acceptance criteria

- [x] `CREATE TABLE`, `INSERT` and `SELECT … WHERE` parse to an AST.
- [x] Operator precedence and associativity are correct at every level.
- [x] Every token and node carries a span that slices back to its own source.
- [x] A parent's span contains every child's span.
- [x] Node ids are unique across a multi-statement script.
- [x] Syntax errors report line, column and what was expected.
- [x] Valid-but-unimplemented SQL is distinguished and names a milestone.
- [x] A reserved word used as a name suggests quoting it.
- [x] Incomplete SQL still yields tokens and a positioned error.
- [x] No input can make the parse endpoint fail.
- [x] Selecting an AST node highlights its source; moving the cursor selects the
      innermost node.
- [x] Parser events appear in the same timeline as storage events.
- [x] Parsing reads no pages and has no side effects.
- [x] There is still no `/query` endpoint.

---

## Known limitations

| Limitation | Resolved by |
|---|---|
| Nothing executes; parsing only | M3 |
| No name resolution — `SELECT nope FROM nope` parses fine | M4, the binder |
| `NULL` has no type until bound | M4 |
| No `ORDER BY`, `LIMIT`, `GROUP BY`, `DISTINCT` | unscheduled |
| No `UPDATE`, `DELETE`, `DROP` | unscheduled |
| No joins, subqueries or functions | M3 builds the operator framework they need |
| No `IN`, `LIKE`, `BETWEEN` | unscheduled |
| `VARCHAR(n)` parses but the length is ignored | needs constraint checking |
| Only one table per `FROM` | needs joins |
| `innermostNodeAt` is O(nodes) per cursor move | fine at statement scale |

Each unsupported construct is *recognised* and reported by name, so none of
these fail with a confusing message.

---

## Next: Milestone 3 — execution engine and operator debugger

**Engine.** Volcano-model operators — sequential scan, filter, projection — plus
expression evaluation over real rows. `POST /query` finally appears, and
`features.execution` flips to true.

**Visualizer.** The physical operator tree, step-through execution one `next()`
call at a time, a current-row inspector, and the operator event stream.

**Demo.** Step through a query and watch rows travel up through the operator
tree one at a time.
