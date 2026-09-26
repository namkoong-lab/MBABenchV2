#!/usr/bin/env python3
"""
Test script to verify multi-action sequential execution with fail-fast behavior.

This test creates a simple Excel file and performs multiple operations in a single iteration
to demonstrate the new multi-action capability.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from excel_cli_agent.mcp_client import ExcelMCPClient
from excel_cli_agent.task_executor import ExcelTaskExecutor


async def test_multi_action_success():
    """Test successful sequential multi-action execution"""
    print("\n" + "="*80)
    print("TEST 1: Multi-Action Success")
    print("="*80 + "\n")

    # Create temp workspace
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"📂 Workspace: {tmpdir}")

        # Initialize client and executor
        server_path = "./excel_mcp_server/server.py"
        client = ExcelMCPClient(server_path, tmpdir)
        executor = ExcelTaskExecutor(client, os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini")
        executor.set_max_iterations(5)

        await client.connect()

        # Test task that should use multiple actions
        task = """Create a simple budget file named test.xlsx with:
1. A worksheet called 'Budget'
2. Add headers in row 1: 'Item' in A1 and 'Amount' in B1
3. Add data: 'Rent' in A2, '1000' in B2
4. Add a formula in B3 that sums B2 (should be =B2 for now)

Try to complete as many steps as possible in a single iteration using multiple actions."""

        print(f"📝 Task: {task}\n")

        result = await executor.execute_task(task)

        await client.disconnect()

        print(f"\n✅ Test Result:")
        print(f"   Status: {result.status}")
        print(f"   Iterations: {result.total_iterations}")
        print(f"   Final Result: {result.final_result}")

        # Check if file was created
        test_file = Path(tmpdir) / "test.xlsx"
        if test_file.exists():
            print(f"   ✅ File created successfully")
        else:
            print(f"   ❌ File not created")

        return result


async def test_multi_action_with_failure():
    """Test multi-action execution with intentional failure (fail-fast)"""
    print("\n" + "="*80)
    print("TEST 2: Multi-Action with Failure (Fail-Fast)")
    print("="*80 + "\n")

    # Create temp workspace
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"📂 Workspace: {tmpdir}")

        # Initialize client and executor
        server_path = "./excel_mcp_server/server.py"
        client = ExcelMCPClient(server_path, tmpdir)
        executor = ExcelTaskExecutor(client, os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini")
        executor.set_max_iterations(10)

        await client.connect()

        # Test task with intentional failure in the middle
        task = """Create a file test2.xlsx and try to:
1. Create a worksheet called 'Sheet1'
2. Add some data to A1
3. Try to add a formula to a NON-EXISTENT worksheet called 'DoesNotExist' (this should fail)
4. Add more data to B1 (this should be skipped after failure)

The goal is to test fail-fast behavior: actions after the failure should be skipped."""

        print(f"📝 Task: {task}\n")

        result = await executor.execute_task(task)

        await client.disconnect()

        print(f"\n✅ Test Result:")
        print(f"   Status: {result.status}")
        print(f"   Iterations: {result.total_iterations}")
        print(f"   Final Result: {result.final_result}")

        return result


async def test_single_action_backwards_compat():
    """Test that single action format still works (backwards compatibility)"""
    print("\n" + "="*80)
    print("TEST 3: Single Action (Backwards Compatibility)")
    print("="*80 + "\n")

    # Create temp workspace
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"📂 Workspace: {tmpdir}")

        # Initialize client and executor
        server_path = "./excel_mcp_server/server.py"
        client = ExcelMCPClient(server_path, tmpdir)
        executor = ExcelTaskExecutor(client, os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini")
        executor.set_max_iterations(5)

        await client.connect()

        # Simple task that will likely use single actions
        task = """Create a file test3.xlsx with one worksheet called 'Data'."""

        print(f"📝 Task: {task}\n")

        result = await executor.execute_task(task)

        await client.disconnect()

        print(f"\n✅ Test Result:")
        print(f"   Status: {result.status}")
        print(f"   Iterations: {result.total_iterations}")

        # Check if file was created
        test_file = Path(tmpdir) / "test3.xlsx"
        if test_file.exists():
            print(f"   ✅ File created successfully")
        else:
            print(f"   ❌ File not created")

        return result


async def main():
    """Run all tests"""
    print("\n" + "="*80)
    print("MULTI-ACTION SEQUENTIAL EXECUTION TESTS")
    print("="*80)

    try:
        # Test 1: Successful multi-action
        result1 = await test_multi_action_success()

        # Test 2: Multi-action with failure (fail-fast)
        # Commented out for now as it requires AI to intentionally fail
        # result2 = await test_multi_action_with_failure()

        # Test 3: Single action backwards compatibility
        result3 = await test_single_action_backwards_compat()

        print("\n" + "="*80)
        print("ALL TESTS COMPLETED")
        print("="*80)
        print(f"\nTest 1 (Multi-Action): {result1.status} in {result1.total_iterations} iterations")
        # print(f"Test 2 (Fail-Fast): {result2.status} in {result2.total_iterations} iterations")
        print(f"Test 3 (Single Action): {result3.status} in {result3.total_iterations} iterations")

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
