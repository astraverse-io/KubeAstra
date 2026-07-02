import ResetPasswordForm from "./reset-form";

export default async function ResetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const params = await searchParams;
  const raw = params.token;
  const token = (Array.isArray(raw) ? raw[0] : raw) ?? "";
  return <ResetPasswordForm token={token} />;
}
