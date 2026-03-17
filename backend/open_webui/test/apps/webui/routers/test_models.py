from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.models.models import ModelForm
from open_webui.routers import models


class TestModels(AbstractPostgresTest):
    BASE_PATH = "/api/v1/models"

    def test_models(self):
        form = ModelForm(
            id="my-model",
            base_model_id="base-model-id",
            name="Hello World",
            meta={
                "profile_image_url": "/static/favicon.png",
                "description": "description",
                "capabilities": None,
            },
            params={},
        )

        with mock_webui_user(id="2", role="user") as user:
            response = self.run_async(models.get_models(user=user, db=self.db))
            assert response.model_dump() == {"items": [], "total": 0}

            created = self.run_async(
                models.create_new_model(form_data=form, user=user, db=self.db)
            )
            assert created.id == "my-model"
            assert created.name == "Hello World"

            response = self.run_async(models.get_models(user=user, db=self.db))
            assert response.total == 1
            assert len(response.items) == 1
            assert response.items[0].id == "my-model"
            assert response.items[0].write_access is True

            fetched = self.run_async(
                models.get_model_by_id(id="my-model", user=user, db=self.db)
            )
            assert fetched.id == "my-model"
            assert fetched.name == "Hello World"
            assert fetched.write_access is True

            deleted = self.run_async(
                models.delete_model_by_id(
                    form_data=models.ModelIdForm(id="my-model"),
                    user=user,
                    db=self.db,
                )
            )
            assert deleted is True

            response = self.run_async(models.get_models(user=user, db=self.db))
            assert response.model_dump() == {"items": [], "total": 0}
