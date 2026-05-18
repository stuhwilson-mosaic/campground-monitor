"""Authentication routes: GET/POST /login, GET /logout."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import COOKIE_NAME, check_credentials, create_session_cookie

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if check_credentials(request.app.state.user_store, username, password):
        cookie_value = create_session_cookie(username)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME,
            value=cookie_value,
            httponly=True,
            samesite="lax",
        )
        return response

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password."},
        status_code=200,
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=COOKIE_NAME)
    return response
