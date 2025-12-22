import logging

from sqlalchemy import MetaData, String, and_, func, select

logger = logging.getLogger(__name__)


def store_database_values(engine, conn):
    """Introspect the session, get all the tables and store their primary key values.

    The result is a dict[table_name, list[pk_tuple]]
    """

    metadata = MetaData()
    metadata.reflect(engine)

    dump = {}
    for table_name, table in metadata.tables.items():
        # Get primary key columns and foreign key columns
        pk_columns = [
            column
            for column in table.columns
            if column.primary_key or len(column.foreign_keys) > 0
        ]

        if not pk_columns:
            # Skip tables without primary keys
            continue

        # Select only primary key columns, cast to string at database level
        pk_columns_as_string = [func.cast(col, String) for col in pk_columns]
        result = conn.execute(select(*pk_columns_as_string))
        try:
            dump[table_name] = [tuple(row) for row in result.fetchall()]
        except Exception as ex:
            raise RuntimeError(f"Could not fetch rows from table {table_name}") from ex

    return dump


def check_database_values(engine, conn, stored_values):
    """Check that only the stored_values are present in the database."""

    metadata = MetaData()
    metadata.reflect(engine)

    # Build a list of (table_name, delete_condition) tuples
    to_be_checked = []

    for table_name, table in metadata.tables.items():
        stored_rows = stored_values.get(table_name, [])

        # Get primary key columns and foreign key columns
        pk_columns = [
            column
            for column in table.columns
            if column.primary_key or len(column.foreign_keys) > 0
        ]

        if not pk_columns:
            logger.warning(f"Table {table_name} has no primary key. Skipping.")
            continue

        # Convert stored rows to a set of primary key tuples for fast lookup
        stored_pk_set = set(stored_rows)

        # create a select statement that would include only rows that are not present
        # in the stored values. It will be not (pk1 == val1 and pk2 == val2 and ...) and not (...)
        row_matcher_conditions = []
        for stored_pk in stored_pk_set:
            # Cast columns to string at database level for comparison
            condition = and_(
                *(
                    func.cast(pk_col, String) == pk_val
                    for pk_col, pk_val in zip(pk_columns, stored_pk)
                )
            )
            # negate the condition to match rows that are not equal
            row_matcher_conditions.append(~condition)

        if row_matcher_conditions:
            non_matching_condition = and_(*row_matcher_conditions)
            to_be_checked.append(
                (table_name, table, non_matching_condition, len(stored_pk_set))
            )
        else:
            # delete everything
            to_be_checked.append((table_name, table, None, len(stored_pk_set)))

    # Try to delete rows with retry mechanism for foreign key constraints
    for table_name, table, where_condition, expected_count in to_be_checked:
        # Execute deletion in a transaction so that we can rollback on failure
        with conn.begin():
            check_stmt = select(func.count()).select_from(table)
            if where_condition is not None:
                check_stmt = check_stmt.where(where_condition)

            count = conn.execute(check_stmt).scalar_one()
            assert (
                count == 0
            ), "Commit within transaction has not worked, extra rows found."
