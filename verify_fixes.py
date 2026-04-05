import os
import unittest

# Import the modules we need to test
import quality_stocks
import enhanced_scanner
from core import momentum_analysis
from core.canslim import a_annual_earnings
from core.canslim import n_new_products

class TestCanslimFixes(unittest.TestCase):

    def test_task1_index_routing(self):
        """Test if quality_stocks handles 'large_cap' and returns lists properly."""
        try:
            # Test A: Should no longer throw ValueError
            tickers = quality_stocks.get_index_tickers('large_cap')
            self.assertIsInstance(tickers, list, "Expected get_index_tickers to return a list.")
            self.assertTrue(len(tickers) > 0, "List of tickers should not be empty.")
            
            # Test B: Should no longer return None
            stocks = quality_stocks.get_quality_stock_list(sectors=['large_cap'])
            self.assertIsNotNone(stocks, "get_quality_stock_list returned None instead of a list!")
            self.assertIsInstance(stocks, list, "Expected get_quality_stock_list to return a list.")
        except ValueError as e:
            self.fail(f"Task 1 Failed - get_index_tickers raised ValueError: {e}")

    def test_task2_csv_export(self):
        """Test if export_results_to_csv crashes on the removed turnover_ratio metric."""
        # Mock opportunity data missing 'turnover_ratio' but containing 'shares_outstanding'
        mock_opportunities = [{
            'symbol': 'AAPL',
            'rs_score': 95.0,
            'total_score': 85.0,
            'scores': {'C': 0.8, 'A': 0.9, 'N': 1.0, 'S': 0.7, 'L': 0.9, 'I': 0.8, 'M': 1.0},
            'metrics': {
                'current_growth': 0.3,
                'annual_growth': 0.25,
                'revenue_growth': 0.2,
                'proximity_to_high': 0.99,
                'shares_outstanding': 15000000000
            }
        }]
        
        test_filename = "test_export_debug.csv"
        try:
            enhanced_scanner.export_results_to_csv(mock_opportunities, filename=test_filename)
            self.assertTrue(os.path.exists(test_filename), "CSV file was not created.")
        except KeyError as e:
            self.fail(f"Task 2 Failed - export_results_to_csv raised KeyError: {e}")
        finally:
            # Cleanup the test file
            if os.path.exists(test_filename):
                os.remove(test_filename)

    def test_task3_redundant_fetcher(self):
        """Test if momentum_analysis is importing the cached get_sp500_tickers."""
        import inspect
        try:
            # Check which module the function actually belongs to now
            module_of_func = inspect.getmodule(momentum_analysis.get_sp500_tickers)
            self.assertIn("index_ticker_fetcher", module_of_func.__name__, 
                          "Task 3 Failed - get_sp500_tickers is still hardcoded in momentum_analysis!")
        except AttributeError:
            self.fail("Task 3 Failed - get_sp500_tickers is missing from momentum_analysis entirely.")

    def test_task5_negative_growth(self):
        """Test if _safe_growth filters out negative previous earnings transitions."""
        # Transitioning from -1.0 to 1.0 should return None, not a positive percentage
        val_a = a_annual_earnings._safe_growth(1.0, -1.0)
        val_n = n_new_products._safe_growth(1.0, -1.0)
        
        self.assertIsNone(val_a, "Task 5 Failed - a_annual_earnings._safe_growth did not return None for negative previous value.")
        self.assertIsNone(val_n, "Task 5 Failed - n_new_products._safe_growth did not return None for negative previous value.")

if __name__ == '__main__':
    print("Running CANSLIM Debug Verification Tests...")
    unittest.main(verbosity=2)