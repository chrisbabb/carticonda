"""Small loss-resistant PCM staging buffer for the host audio device."""

from __future__ import annotations

from array import array


class PcmBuffer:
    """Collect native-endian signed 16-bit samples into mixer-sized chunks.

    Pygame exposes one currently playing sound and one queued sound per channel.
    Samples that arrive while both are occupied remain here for the next host
    frame instead of being discarded.
    """

    def __init__(
        self,
        *,
        chunk_samples: int = 1024,
        max_chunks: int = 12,
    ) -> None:
        if chunk_samples <= 0:
            raise ValueError("chunk_samples must be positive")
        if max_chunks < 3:
            raise ValueError("max_chunks must be at least three")
        self.chunk_samples = int(chunk_samples)
        self.chunk_bytes = self.chunk_samples * 2
        self.max_chunks = int(max_chunks)
        self.max_bytes = self.chunk_bytes * self.max_chunks
        self._pcm = bytearray()
        self.resyncs = 0

    @property
    def pending_samples(self) -> int:
        return len(self._pcm) // 2

    def clear(self) -> None:
        self._pcm.clear()

    def append(self, samples: array) -> bool:
        """Append samples and report whether excessive latency was trimmed."""
        if samples.typecode != "h":
            raise TypeError("PCM samples must use array('h')")
        # Extend directly from the array's native byte view. pygame-ce consumes
        # the same native-endian signed-16 format, so no intermediate bytes
        # allocation or conversion is needed.
        self._pcm.extend(memoryview(samples).cast("B"))
        if len(self._pcm) <= self.max_bytes:
            return False

        # If the host device stops consuming, keep enough recent audio to
        # rebuild a smooth prebuffer. This bounds latency while making the
        # exceptional loss explicit and deterministic.
        keep = self.chunk_bytes * min(4, self.max_chunks - 1)
        del self._pcm[:-keep]
        self.resyncs += 1
        return True

    def pop_chunk(self) -> bytes | None:
        if len(self._pcm) < self.chunk_bytes:
            return None
        result = bytes(self._pcm[:self.chunk_bytes])
        del self._pcm[:self.chunk_bytes]
        return result

    def peek_chunk(self) -> memoryview | None:
        """Borrow the next chunk until the host has copied it."""
        if len(self._pcm) < self.chunk_bytes:
            return None
        return memoryview(self._pcm)[:self.chunk_bytes]

    def discard_chunk(self) -> None:
        """Remove a complete chunk after a borrowed view is released."""
        if len(self._pcm) < self.chunk_bytes:
            raise RuntimeError("no complete PCM chunk is available")
        del self._pcm[:self.chunk_bytes]
