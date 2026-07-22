import AsyncStorage from "@react-native-async-storage/async-storage";

import { SAFE_ENDPOINT } from "@/src/theme";

const DEVICE_ID_KEY = "quakeguard_device_id";

export async function getDeviceId(): Promise<string> {
  let id = await AsyncStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = `qg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    await AsyncStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

export type CheckInStatus = "not_responding" | "safe";

export interface LocationPayload {
  latitude: number | null;
  longitude: number | null;
  accuracy: number | null;
  error: string | null;
}

export interface BatteryPayload {
  level: number | null;
  state: string | null;
}

/**
 * POST a status update to the external safequake endpoint. Fans lat/lng
 * across all common field names so any dashboard schema will find them.
 */
export async function postStatus(opts: {
  status: CheckInStatus;
  location?: LocationPayload;
  battery?: BatteryPayload;
}): Promise<Response> {
  const deviceId = await getDeviceId();
  const { status, location, battery } = opts;

  const payload: Record<string, any> = {
    deviceId,
    status,
    client_name: "quakeguard-mobile",
    timestamp: new Date().toISOString(),
    location: location ?? {
      latitude: null,
      longitude: null,
      accuracy: null,
      error: null,
    },
    battery: battery ?? { level: null, state: null },
    batteryLevel: battery?.level ?? null,
    batteryState: battery?.state ?? null,
  };

  const lat = location?.latitude ?? null;
  const lng = location?.longitude ?? null;
  if (lat !== null && lng !== null) {
    payload.latitude = lat;
    payload.longitude = lng;
    payload.lat = lat;
    payload.lng = lng;
    payload.lon = lng;
    payload.accuracy = location?.accuracy ?? null;
    payload.coords = { latitude: lat, longitude: lng, accuracy: location?.accuracy ?? null };
    payload.coordinates = [lng, lat];
    payload.geo = { type: "Point", coordinates: [lng, lat] };
  }

  console.log(`[QuakeGuard] POST (${status}) →`, JSON.stringify(payload));

  return fetch(SAFE_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
