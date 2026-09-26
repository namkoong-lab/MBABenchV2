# Test fixtures

Captured read-only from the benchmark database so the offline path can be
checked against the shape the cloud path produces, without a connection.

* `tasks_row_task_1.json` — the `tasks` row for task 1, every column. The
  object-store bucket in the two file columns is replaced by `<bucket>`;
  nothing the tests assert depends on its name.
* `task_attempts_columns.json` — `task_attempts` column names in ordinal
  order. A local sink row must carry exactly this set.

Refresh them with a plain SELECT (autocommit, no session settings) when the
schema changes:

    select * from tasks where id = 1;
    select column_name from information_schema.columns
     where table_name = 'task_attempts' order by ordinal_position;
