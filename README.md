# splunk-sqlanalyzer
Analyze database statements with Splunk

## Notes
The example view expects data to be in `index="main" sourcetype="redshift"`.
The code has only been tested with PostgreSQL statements although the underyling [sqlglot](https://github.com/tobymao/sqlglot) library supports more dialects.
The custom command does take `dialect` as a parameter but it's never been tested with other SQL statements.

