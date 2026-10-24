import os
import struct

BROWSER_MP4_MOVFLAGS = "+faststart"
FRAGMENTED_MP4_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof+global_sidx"
_COPY_CHUNK_SIZE = 16 * 1024 * 1024


def is_transient_media_filename(filename):
    """True for remux/part/tmp sidecars that must not be treated as archive VODs."""
    name = os.path.basename(str(filename or "")).lower()
    if not name:
        return True
    if name.endswith(".part") or name.endswith(".tmp"):
        return True
    if ".remux." in name or name.endswith(".remux.mp4"):
        return True
    if ".partial." in name:
        return True
    return False


def is_archive_mp4_filename(filename):
    """True for final archive/clip MP4 names (not remux/part temps)."""
    name = os.path.basename(str(filename or ""))
    if not name.lower().endswith(".mp4"):
        return False
    return not is_transient_media_filename(name)


def _top_level_atoms(fd, file_size):
    atoms = []
    position = 0
    while position + 8 <= file_size:
        header = os.pread(fd, 16, position)
        if len(header) < 8:
            raise ValueError(f"Truncated MP4 atom at byte {position}")
        atom_size = int.from_bytes(header[:4], "big")
        atom_type = header[4:8]
        header_size = 8
        if atom_size == 1:
            if len(header) < 16:
                raise ValueError(f"Truncated extended MP4 atom at byte {position}")
            atom_size = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif atom_size == 0:
            atom_size = file_size - position
        if atom_size < header_size or position + atom_size > file_size:
            raise ValueError(f"Invalid MP4 atom at byte {position}")
        atoms.append((position, atom_type, atom_size))
        position += atom_size
    if position != file_size:
        raise ValueError("Trailing bytes after final MP4 atom")
    return atoms


def top_level_atom_types(file_path, max_atoms=24):
    atoms = []
    fd = os.open(file_path, os.O_RDONLY)
    try:
        file_size = os.fstat(fd).st_size
        position = 0
        while position + 8 <= file_size and len(atoms) < max_atoms:
            header = os.pread(fd, 16, position)
            if len(header) < 8:
                break
            atom_size = int.from_bytes(header[:4], "big")
            atom_type = header[4:8]
            header_size = 8
            if atom_size == 1:
                if len(header) < 16:
                    break
                atom_size = int.from_bytes(header[8:16], "big")
                header_size = 16
            elif atom_size == 0:
                atom_size = file_size - position
            if atom_size < header_size or position + atom_size > file_size:
                break
            atoms.append({
                "type": atom_type.decode("latin1", errors="replace"),
                "position": position,
                "size": atom_size,
            })
            position += atom_size
    finally:
        os.close(fd)
    return atoms


def is_fragmented_mp4(file_path):
    for atom in top_level_atom_types(file_path):
        atom_type = atom.get("type")
        if atom_type in ("moof", "sidx"):
            return True
        if atom_type == "mdat":
            return False
    return False


def finalize_if_fragmented(file_path):
    if is_fragmented_mp4(file_path):
        finalize_browser_mp4(file_path)


def _parse_sidx(sidx):
    if len(sidx) < 40 or sidx[4:8] != b"sidx":
        raise ValueError("Invalid sidx atom")
    version = sidx[8]
    if version == 0:
        first_offset_position = 24
        reference_count_position = 30
    elif version == 1:
        first_offset_position = 28
        reference_count_position = 38
    else:
        raise ValueError(f"Unsupported sidx version: {version}")
    reference_count = struct.unpack_from(">H", sidx, reference_count_position)[0]
    entries_position = reference_count_position + 2
    expected_size = entries_position + reference_count * 12
    if expected_size != len(sidx):
        raise ValueError("Unexpected sidx size")
    reference_sizes = [
        struct.unpack_from(">I", sidx, entries_position + index * 12)[0] & 0x7FFFFFFF
        for index in range(reference_count)
    ]
    return version, first_offset_position, reference_sizes


def finalize_browser_mp4(file_path):
    """Keep FFmpeg's valid video sidx and remove its invalid audio index."""
    fd = os.open(file_path, os.O_RDWR)
    try:
        file_size = os.fstat(fd).st_size
        atoms = _top_level_atoms(fd, file_size)
        first_moof_index = next(
            (index for index, atom in enumerate(atoms) if atom[1] == b"moof"),
            None,
        )
        if first_moof_index is None:
            raise ValueError("Fragmented MP4 has no moof atoms")
        first_moof_position = atoms[first_moof_index][0]
        front_sidx = [atom for atom in atoms[:first_moof_index] if atom[1] == b"sidx"]
        if not front_sidx:
            raise ValueError("Fragmented MP4 has no front sidx")

        first_sidx_position, _, first_sidx_size = front_sidx[0]
        first_sidx_end = first_sidx_position + first_sidx_size
        between = [
            atom for atom in atoms
            if first_sidx_end <= atom[0] < first_moof_position
        ]
        if any(atom[1] != b"sidx" for atom in between):
            raise ValueError("Unexpected atoms between sidx and first fragment")

        mfra = next((atom for atom in atoms[first_moof_index:] if atom[1] == b"mfra"), None)
        if mfra is None:
            raise ValueError("Fragmented MP4 has no mfra trailer")
        mfra_position = mfra[0]
        moof_positions = [
            atom[0] for atom in atoms[first_moof_index:] if atom[1] == b"moof"
        ]
        fragment_sizes = [
            (moof_positions[index + 1] if index + 1 < len(moof_positions) else mfra_position)
            - position
            for index, position in enumerate(moof_positions)
        ]

        first_sidx = bytearray(os.pread(fd, first_sidx_size, first_sidx_position))
        version, first_offset_position, reference_sizes = _parse_sidx(first_sidx)
        if reference_sizes != fragment_sizes:
            raise ValueError("Video sidx does not match MP4 fragment boundaries")
        struct.pack_into(
            ">Q" if version == 1 else ">I",
            first_sidx,
            first_offset_position,
            0,
        )
        os.pwrite(fd, first_sidx, first_sidx_position)

        media_size = mfra_position - first_moof_position
        source_position = first_moof_position
        destination_position = first_sidx_end
        remaining = media_size
        while remaining:
            chunk_size = min(_COPY_CHUNK_SIZE, remaining)
            chunk = os.pread(fd, chunk_size, source_position)
            if len(chunk) != chunk_size:
                raise ValueError("Unexpected EOF while compacting MP4")
            os.pwrite(fd, chunk, destination_position)
            source_position += chunk_size
            destination_position += chunk_size
            remaining -= chunk_size

        os.ftruncate(fd, first_sidx_end + media_size)
        os.fsync(fd)
    finally:
        os.close(fd)
