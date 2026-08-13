import unittest

from timeout_excel import ExcelImportError, parse_filename_range


class FilenameRangeTests(unittest.TestCase):
    def test_official_export_name(self):
        start, end = parse_filename_range("导出时间范围20260701-20260731回复超时记录.xlsx")
        self.assertEqual(start.isoformat(), "2026-07-01")
        self.assertEqual(end.isoformat(), "2026-07-31")

    def test_dashed_dates(self):
        start, end = parse_filename_range("导出时间范围2026-07-01-2026-07-31回复超时记录.xlsx")
        self.assertEqual(start.isoformat(), "2026-07-01")
        self.assertEqual(end.isoformat(), "2026-07-31")

    def test_missing_range(self):
        with self.assertRaises(ExcelImportError):
            parse_filename_range("回复超时记录.xlsx")

    def test_reversed_range(self):
        with self.assertRaises(ExcelImportError):
            parse_filename_range("导出时间范围20260731-20260701回复超时记录.xlsx")


if __name__ == "__main__":
    unittest.main()
