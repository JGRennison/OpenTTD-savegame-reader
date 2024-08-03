import enum
import struct

from collections import defaultdict

from .binreader import (
    BinaryReaderFile,
    BinaryReaderFileBlockMode,
)
from .compression import UNCOMPRESS
from .enums import (
    FieldType,
    FIELD_TYPE_HAS_LENGTH_FIELD,
)
from .exceptions import ValidationException
from .passthrough import PassthroughReader


class Savegame(PassthroughReader):
    def __init__(self, filename):
        self.filename = filename
        self.savegame_version = None
        self.tables = {}
        self.items = defaultdict(dict)

    def _read_table(self, reader):
        """Read a single table from the header."""
        fields = []
        size = 0
        while True:
            type = struct.unpack(">b", reader.read(1))[0]
            size += 1

            if type == 0:
                break

            key_length, index_size = reader.gamma()
            key = reader.read(key_length)
#            print(f"{type}, {key.decode()}\n")
            field_type = FieldType(type & 0xf)
            fields.append((field_type, True if type & FIELD_TYPE_HAS_LENGTH_FIELD else False, key.decode()))

            size += key_length + index_size

        return fields, size

    def _read_substruct(self, reader, tables, key):
        """Check if there are sub-tables and read them too."""
        size = 0

        for field in tables[key]:
            if field[0] == FieldType.STRUCT:
                table_key = f"{key}.{field[2]}"

                tables[table_key], sub_size = self._read_table(reader)
                size += sub_size
                # Check if this table contains any other tables.
                size += self._read_substruct(reader, tables, table_key)

        return size

    def read_all_tables(self, tag, reader):
        """Read all the tables from the header."""
        tables = {}

        tables["root"], size = self._read_table(reader)
        size += self._read_substruct(reader, tables, "root")

        return tables, size

    def _check_tail(self, reader, item):
        try:
            reader.uint8()
        except ValidationException:
            pass
        else:
            raise ValidationException(f"Junk at the end of {item}.")

    def read(self, fp):
        """Read the savegame."""

        reader = BinaryReaderFile(fp)

        compression = reader.read(4)
        self.savegame_version = reader.uint16()
        reader.uint16()

        decompressor = UNCOMPRESS.get(compression)
        if decompressor is None:
            raise ValidationException(f"Unknown savegame compression {compression}.")

        uncompressed = decompressor.open(fp)
        reader = BinaryReaderFileBlockMode(uncompressed)

        while True:
            tag = reader.read(4)
            if len(tag) == 0 or tag == b"\0\0\0\0":
                break
            if len(tag) != 4:
                raise ValidationException("Invalid savegame.")

            tag = tag.decode()

            m = reader.uint8()
            type = m & 0xF
            if type == 0:
                size = (m >> 4) << 24 | reader.uint24()
                if tag == "SLXI":
                    self.read_slxi(tag, reader.read(size))
                else:
                    self.read_item(tag, {}, -1, reader.read(size))
            elif 1 <= type <= 4:
                if type >= 3:  # CH_TABLE or CH_SPARSE_TABLE
                    size = reader.gamma()[0] - 1

                    tables, size_read = self.read_all_tables(tag, reader)
                    if size_read != size:
                        raise ValidationException("Table header size mismatch.")

                    self.tables[tag] = tables
                else:
                    tables = {}

                index = -1
                while True:
                    size = reader.gamma()[0] - 1
                    if size < 0:
                        break
                    if type == 2 or type == 4:
                        index, index_size = reader.gamma()
                        size -= index_size
                    else:
                        index += 1
                    if size != 0:
                        self.read_item(tag, tables, index, reader.read(size))
            else:
                raise ValidationException("Unknown chunk type.")

        self._check_tail(reader, "file")

    def _read_item(self, data, tables, key="root"):
        result = {}

        for field in tables[key]:
            res, data = self.read_field(data, tables, field[0], field[1], field[2], key)
            result[field[2]] = res

        return result, data

    def read_field(self, data, tables, field, is_list, field_name, key):
        if is_list and field != FieldType.STRING:
            length, data = self.read_gamma(data)

            res = []
            for _ in range(length):
                item, data = self.read_field(data, tables, field, False, field_name, key)
                res.append(item)
            return res, data

        if field == FieldType.STRUCT:
            return self._read_item(data, tables, f"{key}.{field_name}")

        return self.READERS[field](self, data)

    def read_item(self, tag, tables, index, data):
        data = memoryview(data)

        table_index = "0" if index == -1 else str(index)

        if tables:
            self.items[tag][table_index], data = self._read_item(data, tables)
            if tag not in ("GSDT", "AIPL"):  # Known chunk with garbage at the end
                if len(data):
                    raise ValidationException(f"Junk at end of chunk {tag}")
        else:
            self.tables[tag] = {"unsupported": ""}

    def read_slxi(self, tag, data):
        data = memoryview(data)

        self.tables[tag] = {"unsupported": ""}

        chunk_version, data = self.read_uint32(data)
        if chunk_version > 0:
            return
        chunk_flags, data = self.read_uint32(data)
        if chunk_flags != 0:
            return

        self.tables[tag] = {"slxi": ""}

        item_count, data = self.read_uint32(data)
        for idx in range(item_count):
            flags, data = self.read_uint32(data)
            version, data = self.read_uint16(data)
            name, data = self.read_string(data)

            flag_names = []
            if (flags & 1) != 0:
                flag_names.append("ignorable unknown")
            if (flags & 2) != 0:
                flag_names.append("ignorable version")
            if (flags & 4) != 0:
                flag_names.append("extra data present")
            if (flags & 8) != 0:
                flag_names.append("chunk ID list present")

            item = {
                "name": name,
                "version": version,
                "flags": flag_names,
            }

            if (flags & 4) != 0:
                extra_data_size, data = self.read_uint32(data)
                if name == "version_label":
                    item["extra_data"] = data[0:extra_data_size].tobytes().decode()
                else:
                    item["extra_data"] = list(data[0:extra_data_size])
                data = data[extra_data_size:]

            if (flags & 8) != 0:
                extra_chunk_count, data = self.read_uint32(data)
                chunks = []
                for _ in range(extra_chunk_count):
                    chunks.append(data[0:4].tobytes().decode())
                    data = data[4:]
                item["chunks"] = chunks

            self.items[tag][str(idx)] = item
