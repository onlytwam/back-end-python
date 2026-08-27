import os
os.environ["DATABASE_URL"] = "postgresql://rndtechies:FpyZgFZ08FXnzNG088MgUsZpAeTSmdOkDcZJZkTlTAwdbYXtNNjcJQQJ99CHACAAAAAAAAAAAAASAZDO2Smx@twampostgres.postgres.database.azure.com:5432/postgres?sslmode=require"
from main import app
print("App imported successfully without syntax errors!")
