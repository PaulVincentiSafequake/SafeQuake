/**
 * Web stub for MapCanvas — returns null.
 *
 * The seismic-map screen's web path uses a list layout instead of a
 * broken map, so this file only exists to satisfy Metro's import
 * resolver on web. The real implementation is in MapCanvas.native.tsx
 * and Metro picks that automatically on iOS/Android.
 *
 * Why a file split at all: react-native-maps imports react-native
 * internals (`Libraries/Utilities/codegenNativeCommands`) that Metro
 * refuses to serve to web bundles, even from inside a Platform.OS
 * guard. So the import path itself has to be platform-conditional.
 */
import type { MapCanvasProps } from "./MapCanvas.types";

export default function MapCanvas(_props: MapCanvasProps): null {
  return null;
}
