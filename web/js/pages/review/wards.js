// The wards of Jammu city as a plain list, for the "Another ward" picker and the alias form. The ward file is
// public and is the same one the map draws. A failed load says so (nothing stands in for the list).

let cached = null;

/** [{ward_no, ward_name}] sorted by number. Loaded once; a failure is not remembered, so Retry works. */
export function loadWardList() {
  if (cached === null) {
    cached = fetch("/data/wards.geojson", { credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error(`wards: ${response.status}`);
        return response.json();
      })
      .then((geo) => geo.features.map((f) => ({ ward_no: f.properties.ward_no, ward_name: f.properties.ward_name })).sort((a, b) => a.ward_no - b.ward_no))
      .catch((err) => {
        cached = null;
        throw err;
      });
  }
  return cached;
}
