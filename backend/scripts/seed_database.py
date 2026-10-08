from app.database import initialize_database


if __name__ == "__main__":
    data = initialize_database(force_seed=True)
    print(f"Seeded {len(data['hospitals'])} hospitals, {len(data['supplies'])} supplies, "
          f"{len(data['inventory'])} inventory batches, and {len(data['demand_history'])} demand observations.")
