package preview

import (
	"bytes"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// CanPreview reports whether the app can prepare an inline preview for a file.
func CanPreview(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".orf", ".nef", ".jpg", ".jpeg":
		return true
	default:
		return false
	}
}

// LoadPreviewBytes returns JPEG bytes for ORF/NEF RAW files or the original
// bytes for JPEG files.
func LoadPreviewBytes(path string) ([]byte, error) {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".jpg", ".jpeg":
		return os.ReadFile(path)
	case ".orf", ".nef":
		return ExtractRAWPreview(path)
	default:
		return nil, errors.New("unsupported preview type")
	}
}

// ExtractRAWPreview extracts an embedded JPEG preview from TIFF-like RAW files.
func ExtractRAWPreview(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if jpeg, err := extractTIFFJPEG(data); err == nil {
		return jpeg, nil
	}
	if jpeg, err := scanJPEG(data); err == nil {
		return jpeg, nil
	}
	return nil, errors.New("embedded JPEG preview not found")
}

func extractTIFFJPEG(data []byte) ([]byte, error) {
	if len(data) < 8 {
		return nil, errors.New("file is too small")
	}

	var order binary.ByteOrder
	switch string(data[:2]) {
	case "II":
		order = binary.LittleEndian
	case "MM":
		order = binary.BigEndian
	default:
		return nil, errors.New("not a TIFF-style file")
	}

	candidates := uniqueOffsets([]uint32{
		order.Uint32(data[4:8]),
		readOptionalOffset(order, data, 8),
	})
	seen := map[uint32]bool{}
	for _, offset := range candidates {
		if offset == 0 || int(offset) >= len(data) {
			continue
		}
		if jpeg, err := walkIFD(data, order, offset, seen, 0); err == nil {
			return jpeg, nil
		}
	}
	return nil, errors.New("TIFF JPEG tags not found")
}

func walkIFD(data []byte, order binary.ByteOrder, offset uint32, seen map[uint32]bool, depth int) ([]byte, error) {
	if depth > 16 || seen[offset] || int(offset)+2 > len(data) {
		return nil, errors.New("invalid IFD")
	}
	seen[offset] = true

	base := int(offset)
	count := int(order.Uint16(data[base : base+2]))
	entryStart := base + 2
	entryEnd := entryStart + count*12
	if entryEnd+4 > len(data) {
		return nil, errors.New("invalid IFD entries")
	}

	var jpegOffset, jpegLength uint32
	var childOffsets []uint32
	for pos := entryStart; pos < entryEnd; pos += 12 {
		tag := order.Uint16(data[pos : pos+2])
		fieldType := order.Uint16(data[pos+2 : pos+4])
		fieldCount := order.Uint32(data[pos+4 : pos+8])
		value := order.Uint32(data[pos+8 : pos+12])

		switch tag {
		case 0x0201: // JPEGInterchangeFormat
			jpegOffset = value
		case 0x0202: // JPEGInterchangeFormatLength
			jpegLength = value
		case 0x014A, 0x8769: // SubIFDs and ExifIFDPointer
			childOffsets = append(childOffsets, valuesAsOffsets(data, order, fieldType, fieldCount, value)...)
		}
	}

	if jpegOffset > 0 && jpegLength > 0 {
		start := int(jpegOffset)
		end := start + int(jpegLength)
		if start >= 0 && end <= len(data) && bytes.HasPrefix(data[start:end], []byte{0xFF, 0xD8}) {
			return data[start:end], nil
		}
	}

	nextOffset := order.Uint32(data[entryEnd : entryEnd+4])
	if nextOffset != 0 {
		childOffsets = append(childOffsets, nextOffset)
	}
	for _, child := range childOffsets {
		if child == 0 || int(child) >= len(data) {
			continue
		}
		if jpeg, err := walkIFD(data, order, child, seen, depth+1); err == nil {
			return jpeg, nil
		}
	}
	return nil, errors.New("JPEG tags not found in IFD")
}

func valuesAsOffsets(data []byte, order binary.ByteOrder, fieldType uint16, count uint32, value uint32) []uint32 {
	size := typeSize(fieldType)
	if size == 0 || count == 0 {
		return nil
	}
	total := int(count) * size
	var raw []byte
	if total <= 4 {
		raw = make([]byte, 4)
		order.PutUint32(raw, value)
		raw = raw[:total]
	} else {
		start := int(value)
		end := start + total
		if start < 0 || end > len(data) {
			return nil
		}
		raw = data[start:end]
	}

	offsets := make([]uint32, 0, count)
	for i := uint32(0); i < count; i++ {
		pos := int(i) * size
		switch fieldType {
		case 3, 8:
			offsets = append(offsets, uint32(order.Uint16(raw[pos:pos+2])))
		case 4, 9, 13:
			offsets = append(offsets, order.Uint32(raw[pos:pos+4]))
		}
	}
	return offsets
}

func typeSize(fieldType uint16) int {
	switch fieldType {
	case 1, 2, 6, 7:
		return 1
	case 3, 8:
		return 2
	case 4, 9, 11, 13:
		return 4
	case 5, 10, 12:
		return 8
	default:
		return 0
	}
}

func readOptionalOffset(order binary.ByteOrder, data []byte, pos int) uint32 {
	if pos+4 > len(data) {
		return 0
	}
	return order.Uint32(data[pos : pos+4])
}

func uniqueOffsets(values []uint32) []uint32 {
	seen := map[uint32]bool{}
	result := make([]uint32, 0, len(values))
	for _, value := range values {
		if !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	return result
}

func scanJPEG(data []byte) ([]byte, error) {
	allowed := map[byte]bool{
		0xDB: true, 0xC0: true, 0xC1: true, 0xC2: true, 0xC4: true, 0xDA: true, 0xFE: true,
	}
	for marker := byte(0xE0); marker <= 0xEF; marker++ {
		allowed[marker] = true
	}

	var best []byte
	searchFrom := 0
	for {
		start := bytes.Index(data[searchFrom:], []byte{0xFF, 0xD8, 0xFF})
		if start == -1 {
			break
		}
		start += searchFrom
		searchFrom = start + 3
		if start+3 >= len(data) || !allowed[data[start+3]] {
			continue
		}
		endRel := bytes.Index(data[start+2:], []byte{0xFF, 0xD9})
		if endRel == -1 {
			continue
		}
		end := start + 2 + endRel + 2
		candidate := data[start:end]
		if len(candidate) < 4096 {
			continue
		}
		if bytes.Contains(candidate[:min(len(candidate), 64)], []byte("JFIF")) ||
			bytes.Contains(candidate[:min(len(candidate), 64)], []byte("Exif")) {
			return candidate, nil
		}
		if len(candidate) > len(best) {
			best = candidate
		}
	}
	if len(best) == 0 {
		return nil, errors.New("JPEG segment not found")
	}
	return best, nil
}
