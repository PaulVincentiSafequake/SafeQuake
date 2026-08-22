/**
 * "Places I care about" — optional named places that also get informational
 * tremor notices (batch 5, B8; approved in place of a user-set radius
 * slider, task #158).
 *
 * Why not a radius slider: distance alone is the wrong variable. A magnitude
 * 6 at 300 km matters more than a magnitude 2 at 20 km. Each saved place is
 * evaluated with the SAME predicted-intensity logic as the user's own
 * location, never a raw radius.
 *
 * HARD SAFETY CONSTRAINT: places affect INFORMATIONAL tremor notices only.
 * They can never filter, delay or suppress the critical alert for the
 * user's own location — the two live on completely separate send paths
 * server-side. This screen says so, plainly, at the top.
 *
 * Location entry uses the OS geocoder (expo-location `geocodeAsync`), so
 * there is no API key and no map to pan: the user types "Catania, Sicily"
 * and confirms the resolved coordinates.
 */
import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Keyboard,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import * as Location from "expo-location";

import { getDeviceId } from "@/src/utils/checkin";

const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? "";

type Place = {
  place_id: string;
  name: string;
  latitude: number;
  longitude: number;
};

/** #247 §3b/§3c (Neo 2026-08-20): each stored place enriched with the
 *  reverse-geocoded description of its coordinates, so the list can
 *  show the actual place name (not raw degrees) and detect the label-
 *  vs-coordinates mismatch that caused the "Athens → Catania" bug in
 *  records saved before the invalidate-on-edit fix landed. */
type EnrichedPlace = Place & {
  /** "Catania, Metropolitan City of Catania, Italy" — the OS's own
   *  answer for what these coords are. null if reverse-geocode was
   *  offline or failed; the row falls back to raw coords in that case
   *  (rule 9.5 — worse-but-working). */
  resolvedAddress: string | null;
  /** The city component of the resolved address alone ("Catania"),
   *  used for the label-vs-coordinates mismatch check. null if the
   *  reverse-geocode did not include a city. */
  resolvedCity: string | null;
  /** True when the stored label doesn't contain the resolved city and
   *  vice versa. Records saved before the #247 fix commonly hit this
   *  ("Athens" stored at Catania's coords). */
  looksWrong: boolean;
};

function normaliseForCompare(s: string | null | undefined): string {
  return String(s ?? "").trim().toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

export default function PlacesScreen() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [places, setPlaces] = useState<Place[]>([]);
  /** #247 §3b: reverse-geocoded copy of `places`, computed after every
   *  load. Renders in the list so users see "Athens — Catania, Italy"
   *  instead of two degree numbers they can't check. */
  const [enriched, setEnriched] = useState<EnrichedPlace[]>([]);
  const [maxPlaces, setMaxPlaces] = useState(5);
  const [enabled, setEnabled] = useState(true);

  const [name, setName] = useState("");
  const [search, setSearch] = useState("");
  const [resolved, setResolved] = useState<{
    latitude: number;
    longitude: number;
    /** The exact text the user searched when Find succeeded. If the
     * search text later changes, resolved is cleared so the user cannot
     * save a "Catania" label against Athens coordinates (#247). */
    searchedAs: string;
    /** Reverse-geocoded description of the coords (e.g. "Catania,
     * Metropolitan City of Catania, Italy") so the user can see what
     * the OS geocoder actually chose before committing. */
    address: string | null;
  } | null>(null);
  const [busy, setBusy] = useState<"find" | "save" | null>(null);

  const load = useCallback(async () => {
    try {
      const did = await getDeviceId();
      const r = await fetch(`${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/places`);
      if (r.ok) {
        const data = await r.json();
        setPlaces(data.places ?? []);
        setEnabled(data.enabled !== false);
        if (data.max_places) setMaxPlaces(data.max_places);
      }
    } catch {
      // offline — show an empty list rather than an error wall
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** #247 §3b/§3c: reverse-geocode every stored place after load, so
   *  the list shows the real address and mismatches surface. Runs
   *  best-effort: if reverse-geocode is offline or fails, the row
   *  falls back to raw coords (rule 9.5 worse-but-working). */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const results: EnrichedPlace[] = [];
      for (const p of places) {
        let resolvedAddress: string | null = null;
        let resolvedCity: string | null = null;
        try {
          const parts = await Location.reverseGeocodeAsync({
            latitude: p.latitude, longitude: p.longitude,
          });
          if (parts.length) {
            const first = parts[0];
            resolvedCity = first.city ?? first.subregion ?? null;
            resolvedAddress = [resolvedCity, first.region, first.country]
              .filter((s) => !!s).join(", ") || null;
          }
        } catch { /* offline — leave both null */ }
        const nName = normaliseForCompare(p.name);
        const nCity = normaliseForCompare(resolvedCity);
        // A record "looks wrong" when we HAVE a resolved city and it
        // shares no substring with the stored label. This catches
        // "Athens" at Catania's coords cleanly; it deliberately
        // tolerates personal labels like "Mum's house" (nCity="Msida"
        // but label contains no city substring — flagged; the user
        // can dismiss with Keep on the migration prompt).
        const looksWrong = !!resolvedCity && nName !== "" && nCity !== ""
          && !nName.includes(nCity) && !nCity.includes(nName);
        results.push({ ...p, resolvedAddress, resolvedCity, looksWrong });
      }
      if (!cancelled) setEnriched(results);
    })();
    return () => { cancelled = true; };
  }, [places]);

  const findLocation = async () => {
    const query = search.trim();
    if (!query) return;
    Keyboard.dismiss();
    setBusy("find");
    setResolved(null);
    try {
      const hits = await Location.geocodeAsync(query);
      if (!hits.length) {
        Alert.alert(
          "Couldn't find that",
          "Try a town or city with its region — for example “Catania, Sicily”.",
        );
        return;
      }
      const { latitude, longitude } = hits[0];
      // Reverse-geocode so the user sees what the OS actually chose.
      // #247: users typed one city, the geocoder returned another, and
      // the mismatch was only visible in the tiny "lat°, lng°" preview.
      // Now the resolved address is shown in words so a wrong hit is
      // obvious before Save.
      let address: string | null = null;
      let suggestedName: string | null = null;
      try {
        const parts = await Location.reverseGeocodeAsync({ latitude, longitude });
        if (parts.length) {
          const p = parts[0];
          address = [p.city ?? p.subregion, p.region, p.country]
            .filter((s) => !!s && s !== "")
            .join(", ") || null;
          // #248 (Batch 7 D): the OS's real city name is a better
          // auto-suggested label than the raw search token.
          if (p.city || p.subregion) {
            suggestedName = String(p.city || p.subregion).slice(0, 40);
          }
        }
      } catch {
        // Reverse geocode is a UX aid, not a safety gate. If it fails
        // we still show the coordinates — the user can always tap Find
        // again to re-check.
      }
      setResolved({ latitude, longitude, searchedAs: query, address });
      if (!name.trim()) {
        setName(suggestedName || query.split(",")[0].trim().slice(0, 40));
      }
    } catch {
      Alert.alert(
        "Couldn't look that up",
        "Check your connection and try again.",
      );
    } finally {
      setBusy(null);
    }
  };

  const savePlace = async () => {
    if (!resolved || !name.trim()) return;
    setBusy("save");
    try {
      const did = await getDeviceId();
      const r = await fetch(
        `${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/places`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name.trim(),
            latitude: resolved.latitude,
            longitude: resolved.longitude,
          }),
        },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${r.status}`);
      }
      setName("");
      setSearch("");
      setResolved(null);
      await load();
    } catch (e) {
      Alert.alert("Could not save", (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const removePlace = (place: Place) => {
    Alert.alert(
      `Remove ${place.name}?`,
      "You'll stop getting tremor notices for this place.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Remove",
          style: "destructive",
          onPress: async () => {
            try {
              const did = await getDeviceId();
              await fetch(
                `${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/places/${place.place_id}`,
                { method: "DELETE" },
              );
              await load();
            } catch {
              Alert.alert("Could not remove", "Please try again.");
            }
          },
        },
      ],
    );
  };

  const toggleEnabled = async (next: boolean) => {
    setEnabled(next);
    try {
      const did = await getDeviceId();
      await fetch(
        `${BACKEND_URL}/api/devices/${encodeURIComponent(did)}/places/enabled`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        },
      );
    } catch {
      setEnabled(!next);
      Alert.alert("Could not save", "Please check your connection and try again.");
    }
  };

  const atLimit = places.length >= maxPlaces;

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <View style={styles.header}>
        <Pressable
          style={styles.backBtn}
          onPress={() => router.back()}
          hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
          accessibilityRole="button"
          accessibilityLabel="Back"
        >
          <Ionicons name="chevron-back" size={26} color="#E7EDF5" />
        </Pressable>
        <Text style={styles.title}>Places I care about</Text>
        <View style={{ width: 26 }} />
      </View>

      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <ScrollView
          contentContainerStyle={styles.scroll}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <View style={styles.rulePanel}>
            <Ionicons name="shield-checkmark" size={22} color="#1F8A3A" />
            <Text style={styles.ruleText}>
              Get tremor notices for somewhere else you care about — family in
              Sicily, a second home.{"\n"}
              <Text style={styles.ruleTextBold}>
                This never changes the emergency alert for where you are. That
                one is set to come through Focus and silent.
              </Text>
            </Text>
          </View>

          {loading ? (
            <ActivityIndicator style={{ marginTop: 40 }} color="#5DB1FF" />
          ) : (
            <>
              {places.length > 0 && (
                <View style={styles.switchRow}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.switchLabel}>Notices for saved places</Text>
                    <Text style={styles.switchHelper}>
                      Turn off to silence all of them without deleting any.
                    </Text>
                  </View>
                  <Switch
                    value={enabled}
                    onValueChange={toggleEnabled}
                    trackColor={{ true: "#1F8A3A", false: "#3A4558" }}
                    thumbColor="#E7EDF5"
                  />
                </View>
              )}

              {places.length === 0 ? (
                <Text style={styles.empty}>
                  You haven&apos;t saved any places. This is optional — nothing
                  changes if you skip it.
                </Text>
              ) : (
                <View style={styles.list}>
                  {enriched.map((p) => (
                    <View
                      key={p.place_id}
                      style={[
                        styles.placeRow,
                        p.looksWrong && styles.placeRowWarning,
                      ]}
                      testID={`place-${p.place_id}`}
                    >
                      <Ionicons
                        name={p.looksWrong ? "warning" : "location"}
                        size={20}
                        color={p.looksWrong ? "#F4C842" : "#5DB1FF"}
                      />
                      <View style={{ flex: 1 }}>
                        <Text style={styles.placeName}>{p.name}</Text>
                        {/* #247 §3b (Neo 2026-08-20): resolved place name
                            replaces raw coordinates in the list, so the
                            "Athens at Catania" bug is visible in one
                            glance. Coordinates only shown as a fallback
                            when reverse-geocode couldn't answer. */}
                        {p.resolvedAddress ? (
                          <Text style={styles.placeCoords}>
                            {p.resolvedAddress}
                          </Text>
                        ) : (
                          <Text style={styles.placeCoords}>
                            {p.latitude.toFixed(3)}°, {p.longitude.toFixed(3)}°
                            <Text style={styles.placeCoordsHint}>
                              {" "}(couldn&apos;t look up the place name — offline?)
                            </Text>
                          </Text>
                        )}
                        {/* #247 §3a (Neo 2026-08-20): migration prompt
                            for records saved before the invalidate-on-
                            edit fix. Shows the mismatch, names both
                            places, and offers Keep or Remove — never
                            silently corrects, never silently deletes. */}
                        {p.looksWrong && (
                          <View style={styles.mismatchNotice}>
                            <Text style={styles.mismatchText}>
                              &ldquo;{p.name}&rdquo; is saved with a location in{" "}
                              <Text style={styles.mismatchTextBold}>
                                {p.resolvedAddress}
                              </Text>
                              . That looks wrong.
                            </Text>
                            <View style={styles.mismatchActions}>
                              <Pressable
                                style={styles.mismatchBtnKeep}
                                onPress={() => Alert.alert(
                                  "Kept as-is",
                                  `"${p.name}" will keep pointing at ${p.resolvedAddress}. If this was intentional (e.g. a nickname), that's fine.`,
                                )}
                                testID={`place-${p.place_id}-keep`}
                              >
                                <Text style={styles.mismatchBtnKeepText}>Keep</Text>
                              </Pressable>
                              <Pressable
                                style={styles.mismatchBtnRemove}
                                onPress={() => removePlace(p)}
                                testID={`place-${p.place_id}-remove-wrong`}
                              >
                                <Text style={styles.mismatchBtnRemoveText}>Remove</Text>
                              </Pressable>
                            </View>
                          </View>
                        )}
                      </View>
                      <Pressable
                        onPress={() => removePlace(p)}
                        hitSlop={12}
                        style={styles.removeBtn}
                        accessibilityRole="button"
                        accessibilityLabel={`Remove ${p.name}`}
                      >
                        <Ionicons name="trash-outline" size={20} color="#E06C6C" />
                      </Pressable>
                    </View>
                  ))}
                </View>
              )}

              <Text style={styles.sectionTitle}>Add a place</Text>
              {atLimit ? (
                <Text style={styles.empty}>
                  You&apos;ve saved the maximum of {maxPlaces} places. Remove one
                  to add another.
                </Text>
              ) : (
                <View style={styles.addCard}>
                  <Text style={styles.inputLabel}>Town or city</Text>
                  <View style={styles.searchRow}>
                    <TextInput
                      value={search}
                      onChangeText={(t) => {
                        setSearch(t);
                        // #247 fix: any edit to the search text invalidates
                        // the previously-resolved coordinates. Without this,
                        // a user could type "Catania", tap Find, then change
                        // the text to "Athens" and tap Save — and the saved
                        // coordinates would still be Catania's.
                        if (resolved && t.trim() !== resolved.searchedAs) {
                          setResolved(null);
                        }
                      }}
                      placeholder="Catania, Sicily"
                      placeholderTextColor="#61708A"
                      style={styles.input}
                      autoCorrect={false}
                      returnKeyType="search"
                      onSubmitEditing={findLocation}
                      testID="place-search-input"
                    />
                    <Pressable
                      onPress={findLocation}
                      disabled={busy !== null || !search.trim()}
                      style={({ pressed }) => [
                        styles.findBtn,
                        (busy !== null || !search.trim()) && styles.btnDisabled,
                        pressed && { opacity: 0.85 },
                      ]}
                      testID="place-find-btn"
                    >
                      {busy === "find" ? (
                        <ActivityIndicator color="#0B1220" />
                      ) : (
                        <Text style={styles.findBtnText}>Find</Text>
                      )}
                    </Pressable>
                  </View>

                  {resolved && (
                    <>
                      {(() => {
                        // §3c #247 (Neo 2026-08-20): if the resolved
                        // address doesn't contain the searched term
                        // (e.g. "Paphos" → "Polemi, Paphos, Cyprus" —
                        // a village near Paphos, not Paphos itself),
                        // frame it as a question rather than a flat
                        // statement, so the user can catch the miss.
                        const searchLower = resolved.searchedAs.toLowerCase();
                        const addressLower = String(resolved.address ?? "").toLowerCase();
                        const looksApproximate =
                          !!resolved.address &&
                          !addressLower.includes(searchLower) &&
                          !searchLower.includes(addressLower.split(",")[0].trim());
                        return (
                          <View
                            style={[
                              styles.resolvedCard,
                              looksApproximate && styles.resolvedCardWarning,
                            ]}
                            testID="place-resolved-card"
                          >
                            <Ionicons
                              name={looksApproximate ? "help-circle" : "location"}
                              size={16}
                              color={looksApproximate ? "#F4C842" : "#B3E5C4"}
                            />
                            <View style={{ flex: 1 }}>
                              <Text style={styles.resolvedHeader}>
                                {looksApproximate
                                  ? `You searched for ${resolved.searchedAs}. The closest match we found is:`
                                  : "Found this place"}
                              </Text>
                              {resolved.address ? (
                                <Text style={styles.resolvedAddress}>
                                  {resolved.address}
                                </Text>
                              ) : null}
                              <Text style={styles.resolvedText}>
                                {resolved.latitude.toFixed(3)}°,{" "}
                                {resolved.longitude.toFixed(3)}°
                              </Text>
                              <Text style={styles.resolvedHint}>
                                {looksApproximate
                                  ? "Is that the place you meant? If not, refine the search above and tap Find again."
                                  : "Not the right place? Change the search above and tap Find again."}
                              </Text>
                            </View>
                          </View>
                        );
                      })()}
                      <Text style={styles.inputLabel}>Call it</Text>
                      <TextInput
                        value={name}
                        onChangeText={setName}
                        placeholder="Mum's house"
                        placeholderTextColor="#61708A"
                        style={styles.input}
                        maxLength={40}
                        testID="place-name-input"
                      />
                      <Pressable
                        onPress={savePlace}
                        disabled={busy !== null || !name.trim()}
                        style={({ pressed }) => [
                          styles.saveBtn,
                          (busy !== null || !name.trim()) && styles.btnDisabled,
                          pressed && { opacity: 0.85 },
                        ]}
                        testID="place-save-btn"
                      >
                        {busy === "save" ? (
                          <ActivityIndicator color="#0B1220" />
                        ) : (
                          <Text style={styles.saveBtnText}>Save this place</Text>
                        )}
                      </Pressable>
                    </>
                  )}
                </View>
              )}

              <Text style={styles.footer}>
                Notices for a saved place always name the place, so you know
                straight away it isn&apos;t about you. We use the same rule
                for a saved place as we do for where you are — how strongly
                it would be felt there, not how far away it is.
              </Text>
            </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0B1220" },
  header: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: "#25324A",
  },
  backBtn: { padding: 4 },
  title: { color: "#E7EDF5", fontSize: 18, fontWeight: "700" },
  scroll: { padding: 20, paddingBottom: 60 },

  rulePanel: {
    flexDirection: "row", gap: 10, alignItems: "flex-start",
    backgroundColor: "#0F2818", borderColor: "#1F8A3A", borderWidth: 1,
    borderRadius: 12, padding: 14, marginBottom: 20,
  },
  ruleText: { flex: 1, color: "#B3E5C4", fontSize: 14, lineHeight: 20 },
  ruleTextBold: { fontWeight: "700", color: "#E7EDF5" },

  switchRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    backgroundColor: "#151E2F", borderRadius: 12, padding: 16,
    borderWidth: 1, borderColor: "#25324A", marginBottom: 16,
  },
  switchLabel: { color: "#E7EDF5", fontSize: 15, fontWeight: "700" },
  switchHelper: { color: "#8FA0BC", fontSize: 14, marginTop: 2, lineHeight: 17 },

  empty: { color: "#8FA0BC", fontSize: 14, lineHeight: 19, marginBottom: 8 },
  list: { gap: 10, marginBottom: 8 },
  placeRow: {
    flexDirection: "row", alignItems: "flex-start", gap: 12,
    backgroundColor: "#151E2F", borderRadius: 12, padding: 14,
    borderWidth: 1, borderColor: "#25324A",
  },
  placeRowWarning: {
    borderColor: "#F4C842",
    borderWidth: 1.5,
    backgroundColor: "#241C08",
  },
  placeName: { color: "#E7EDF5", fontSize: 16, fontWeight: "700" },
  placeCoords: { color: "#8FA0BC", fontSize: 14, marginTop: 2, lineHeight: 18 },
  placeCoordsHint: { color: "#61708A", fontSize: 14, fontStyle: "italic" },
  mismatchNotice: {
    marginTop: 10, padding: 10, borderRadius: 8,
    backgroundColor: "rgba(244,200,66,0.08)",
    borderWidth: 1, borderColor: "rgba(244,200,66,0.35)",
  },
  mismatchText: { color: "#E7EDF5", fontSize: 14, lineHeight: 20 },
  mismatchTextBold: { color: "#F4C842", fontWeight: "700" },
  mismatchActions: { flexDirection: "row", gap: 8, marginTop: 10 },
  mismatchBtnKeep: {
    flex: 1, paddingVertical: 10, borderRadius: 6,
    backgroundColor: "#25324A", alignItems: "center",
  },
  mismatchBtnKeepText: { color: "#E7EDF5", fontSize: 14, fontWeight: "700" },
  mismatchBtnRemove: {
    flex: 1, paddingVertical: 10, borderRadius: 6,
    backgroundColor: "#5A1414", alignItems: "center",
  },
  mismatchBtnRemoveText: { color: "#FFD4D4", fontSize: 14, fontWeight: "700" },
  removeBtn: { minWidth: 44, minHeight: 44, alignItems: "center", justifyContent: "center" },

  sectionTitle: {
    color: "#E7EDF5", fontSize: 15, fontWeight: "700",
    marginTop: 24, marginBottom: 10,
  },
  addCard: {
    backgroundColor: "#151E2F", borderRadius: 12, padding: 16,
    borderWidth: 1, borderColor: "#25324A", gap: 10,
  },
  inputLabel: { color: "#8FA0BC", fontSize: 12, fontWeight: "700" },
  searchRow: { flexDirection: "row", gap: 10, alignItems: "center" },
  input: {
    flex: 1, minHeight: 48,
    backgroundColor: "#0B1220", borderRadius: 10,
    borderWidth: 1, borderColor: "#25324A",
    paddingHorizontal: 12, color: "#E7EDF5", fontSize: 16,
  },
  findBtn: {
    minHeight: 48, minWidth: 78, borderRadius: 10, backgroundColor: "#5DB1FF",
    alignItems: "center", justifyContent: "center", paddingHorizontal: 16,
  },
  findBtnText: { color: "#0B1220", fontSize: 16, fontWeight: "800" },
  resolvedCard: {
    flexDirection: "row", gap: 10, alignItems: "flex-start",
    backgroundColor: "#0F2818", borderColor: "#1F8A3A", borderWidth: 1,
    borderRadius: 10, padding: 12, marginVertical: 4,
  },
  resolvedCardWarning: {
    backgroundColor: "#241C08",
    borderColor: "#F4C842",
    borderWidth: 1.5,
  },
  resolvedHeader: {
    color: "#B3E5C4", fontSize: 12, fontWeight: "800", marginBottom: 4,
  },
  resolvedAddress: {
    color: "#E7EDF5", fontSize: 15, fontWeight: "700", marginBottom: 2,
  },
  resolvedText: { color: "#B3E5C4", fontSize: 14 },
  resolvedHint: {
    color: "#8FA0BC", fontSize: 14, fontStyle: "italic",
    marginTop: 6, lineHeight: 18,
  },
  saveBtn: {
    minHeight: 50, borderRadius: 10, backgroundColor: "#1F8A3A",
    alignItems: "center", justifyContent: "center", marginTop: 4,
  },
  saveBtnText: { color: "#FFFFFF", fontSize: 16, fontWeight: "800" },
  btnDisabled: { opacity: 0.45 },

  footer: {
    color: "#8FA0BC", fontSize: 14, fontStyle: "italic",
    textAlign: "center", marginTop: 24, lineHeight: 18,
  },
});
