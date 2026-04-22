import { MapPin } from "lucide-react";
import { SectionHeader } from "@/components/Grid";
import { useSpotifyArtistStats } from "@/hooks/useSpotifyEnrichment";

/**
 * Top listening cities for an artist, sourced from Spotify's
 * queryArtistOverview GraphQL (the same data shown on the Spotify
 * web player's artist page). Renders as a horizontal row of five
 * cards — one per city — with the city + country and the
 * listener count.
 *
 * Hides itself entirely when Spotify can't resolve the artist
 * (requires at least one track by the artist to have an ISRC we
 * can map to Spotify). Gracefully degrades to nothing on fetch
 * failure.
 */
export function ArtistTopCities({
  artistId,
  sampleIsrc,
}: {
  artistId: string;
  sampleIsrc: string | null;
}) {
  const stats = useSpotifyArtistStats(artistId, sampleIsrc);
  const cities = stats?.top_cities ?? [];
  if (cities.length === 0) return null;

  return (
    <>
      <SectionHeader title="Where fans listen" />
      <div className="mb-8 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        {cities.map((c, i) => (
          <div
            key={`${c.city}-${c.country}-${i}`}
            className="flex flex-col gap-1 rounded-lg border border-border/40 bg-card/40 px-4 py-3"
          >
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              <MapPin className="h-3 w-3" />
              <span className="truncate">
                {c.country ? `${c.city}, ${c.country}` : c.city}
              </span>
            </div>
            <div className="text-lg font-bold tabular-nums">
              {c.listeners.toLocaleString()}
            </div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              listeners
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
