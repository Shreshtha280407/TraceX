/** A parsed evidence record always comes back from the backend as a plain JSON
 * object (app/engine/adapters/source.py normalizes every format to a dict before
 * it ever reaches the API) — so a record sourced from a CSV row looked identical
 * to one sourced from JSON/XML: an escaped JSON blob. That's correct for JSON/XML,
 * but wrong for CSV, which has no nesting and reads far better as a field/value
 * table. `isCsv` (derived from source_format / locator_type upstream) picks the
 * rendering; the underlying data is unchanged either way. */
export function RecordPreview({
  record,
  isCsv,
  preClassName = "",
}: {
  record: unknown;
  isCsv: boolean;
  preClassName?: string;
}) {
  if (isCsv && record && typeof record === "object" && !Array.isArray(record)) {
    const entries = Object.entries(record as Record<string, unknown>);
    return (
      <table className="record-table">
        <tbody>
          {entries.map(([key, value]) => (
            <tr key={key}>
              <th>{key}</th>
              <td>{String(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return <pre className={preClassName}>{JSON.stringify(record, null, 2)}</pre>;
}
